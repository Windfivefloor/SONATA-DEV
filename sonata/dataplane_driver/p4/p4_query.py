#!/usr/bin/env python
# Author: Ruediger Birkner (Networked Systems Group at ETH Zurich)


from p4_elements import Action, Header, Table
# TODO: Fix these imports
from p4_operators import P4Distinct, P4Filter, P4Map, P4MapInit, P4Reduce, QID_SIZE, COUNT_SIZE,INDEX_SIZE
from p4_primitives import ModifyField, AddHeader
from sonata.dataplane_driver.utils import get_logger
from p4_field import P4Field
from p4_layer import P4Layer
from p4_layer import OutHeaders
import logging


# Class that holds one refined query - which consists of an ordered list of operators
class P4Query(object):
    all_fields = []
    out_header = None
    out_header_table = None
    query_drop_action = None
    satisfied_table = None

    def __init__(self, query_id, parse_payload, payload_fields, read_register, generic_operators, nop_name, drop_meta_field,
                 satisfied_meta_field, clone_meta_field, p4_raw_fields):

        # LOGGING
        log_level = logging.ERROR
        self.logger = get_logger('P4Query - %i' % query_id, 'INFO')
        self.logger.setLevel(log_level)
        self.logger.info('init')
        self.id = query_id
        self.parse_payload = parse_payload
        self.payload_fields = payload_fields
        self.read_register = read_register
        self.meta_init_name = ''
        print '$$$$$$$$$$$$$ vals: ' + str(self.parse_payload) + ":" + str(self.read_register)

        self.src_to_filter_operator = dict()

        self.nop_action = nop_name

        self.drop_meta_field = '%s_%i' % (drop_meta_field, self.id)
        self.satisfied_meta_field = '%s_%i' % (satisfied_meta_field, self.id)
        self.clone_meta_field = clone_meta_field

        self.p4_raw_fields = p4_raw_fields

        self.actions = dict()

        # general drop action which is applied when a packet doesn't satisfy this query
        self.add_general_drop_action()

        # action and table to mark query as satisfied at end of query processing in ingress
        self.mark_satisfied()

        # initialize operators
        self.get_all_fields(generic_operators)

        self.operators = self.init_operators(generic_operators)

        # create an out header layer
        self.create_out_header()

        # action and table to populate out_header in egress
        self.append_out_header()

    def mark_satisfied(self):
        primitives = list()
        primitives.append(ModifyField(self.satisfied_meta_field, 1))
        primitives.append(ModifyField(self.clone_meta_field, 1))
        self.actions['satisfied'] = Action('do_mark_satisfied_%i' % self.id, primitives)
        self.satisfied_table = Table('mark_satisfied_%i' % self.id, self.actions['satisfied'].get_name(), [], None, 1)

    def add_general_drop_action(self):
        self.actions['drop'] = Action('drop_%i' % self.id, (ModifyField(self.drop_meta_field, 1)))
        self.query_drop_action = self.actions['drop'].get_name()

    def create_out_header(self):
        out_header_name = 'out_header_%i' % self.id
        self.out_header = OutHeaders(out_header_name)
        print "Last Operator", self.operators[-1], self.payload_fields+['ts', 'count']
        sonata_field_list = filter(lambda x: x not in self.payload_fields+['ts', 'count', 'index'], self.operators[-1].get_out_headers())
        out_header_fields = [self.p4_raw_fields.get_target_field(x) for x in sonata_field_list]

        qid_field = P4Field(layer=self.out_header, target_name="qid", sonata_name="qid", size=QID_SIZE)
        out_header_fields = [qid_field] + out_header_fields
        if 'count' in self.operators[-1].get_out_headers():
            out_header_fields.append(P4Field(layer=self.out_header, target_name="count", sonata_name="count",
                                             size=COUNT_SIZE))
        if 'index' in self.operators[-1].get_out_headers():
            out_header_fields.append(P4Field(layer=self.out_header, target_name="index", sonata_name="index",
                                             size=INDEX_SIZE))

        # Add fields to this out header
        self.out_header.fields = out_header_fields

    def append_out_header(self):
        primitives = list()
        primitives.append(AddHeader(self.out_header.get_name()))
        for fld in self.out_header.fields:
            primitives.append(ModifyField('%s.%s' % (self.out_header.get_name(), fld.target_name.replace(".", "_")),
                                      '%s.%s' % (self.meta_init_name, fld.target_name.replace(".", "_"))))
        self.actions['append_out_header'] = Action('do_add_out_header_%i' % self.id, primitives)
        self.out_header_table = Table('add_out_header_%i' % self.id, self.actions['append_out_header'].get_name(), [],
                                      None, 1)

    def get_all_fields(self, generic_operators):
        # TODO: only select fields over which we perform any action
        all_fields = set()
        for operator in generic_operators:
            if operator.name in {'Filter', 'Map', 'Reduce', 'Distinct'}:
                all_fields = all_fields.union(set(operator.get_init_keys()))
        # TODO remove this
        self.all_fields = filter(lambda x: x not in self.payload_fields+['ts', 'count'], all_fields)

    def get_init_fields(self, generic_operators):
        # TODO: only select fields over which we perform any action
        print "#DEBUG INIT FIELDS:", generic_operators
        all_fields = set()
        for operator in generic_operators:
            if operator.name in {'Map', 'Reduce', 'Distinct', 'Filter'}:
                print "#DEBUG INIT FIELDS:", operator.name, operator.get_init_keys()
                all_fields = all_fields.union(set(operator.get_init_keys()))
        # No need to filter out count field
        return filter(lambda x: x not in self.payload_fields+['ts'], all_fields)

    def init_operators(self, generic_operators):
        p4_operators = list()
        operator_id = 1

        map_init_keys = ['qid'] + self.get_init_fields(generic_operators)

        if self.read_register: map_init_keys += ['index']

        print "For Query", self.id, "MapInit fields", map_init_keys

        self.logger.debug('add map_init with keys: %s' % (', '.join(map_init_keys),))
        map_init_operator = P4MapInit(self.id, operator_id, map_init_keys, self.p4_raw_fields)
        self.meta_init_name = map_init_operator.get_meta_name()
        p4_operators.append(map_init_operator)

        # add all the other operators one after the other
        for operator in generic_operators:
            self.logger.debug('add %s operator' % (operator.name,))
            operator_id += 1

            # TODO: Confirm if this is the right way
            keys = filter(lambda x: x != 'payload' and x != 'ts', operator.keys)
            operator.keys = keys
            # TODO: Confirm if this is the right way

            if operator.name == 'Filter':
                match_action = self.nop_action
                miss_action = self.query_drop_action
                filter_operator = P4Filter(self.id,
                                           operator_id,
                                           operator.keys,
                                           operator.filter_keys,
                                           operator.func,
                                           operator.src,
                                           match_action,
                                           miss_action, self.p4_raw_fields)
                if operator.src != 0:
                    self.src_to_filter_operator[operator.src] = filter_operator
                p4_operators.append(filter_operator)

            elif operator.name == 'Map':
                p4_operators.append(P4Map(self.id,
                                          operator_id,
                                          self.meta_init_name,
                                          operator.keys,
                                          operator.map_keys,
                                          operator.func, self.p4_raw_fields))

            elif operator.name == 'Reduce':
                p4_operators.append(P4Reduce(self.id,
                                             operator_id,
                                             self.meta_init_name,
                                             self.query_drop_action,
                                             operator.keys,
                                             operator.threshold,
                                             self.read_register,
                                             self.p4_raw_fields))

            elif operator.name == 'Distinct':
                p4_operators.append(P4Distinct(self.id,
                                               operator_id,
                                               self.meta_init_name,
                                               self.query_drop_action,
                                               self.nop_action,
                                               operator.keys, self.p4_raw_fields))

            else:
                self.logger.error('tried to add an unsupported operator: %s' % operator.name)
        return p4_operators

    def get_ingress_control_flow(self, indent_level):
        curr_indent_level = indent_level

        indent = '\t' * curr_indent_level
        out = '%s// query %i\n' % (indent, self.id)
        # apply one operator after another
        for operator in self.operators:
            indent = '\t' * curr_indent_level
            curr_indent_level += 1
            out += '%sif (%s != 1) {\n' % (indent, self.drop_meta_field)
            out += operator.get_control_flow(curr_indent_level)

        # mark packet as satisfied if it has never been marked as dropped
        indent = '\t' * curr_indent_level
        out += '%sif (%s != 1) {\n' % (indent, self.drop_meta_field)
        out += '%s\tapply(%s);\n' % (indent, self.satisfied_table.get_name())
        out += '%s}\n' % indent

        # close brackets
        for _ in self.operators:
            curr_indent_level -= 1
            indent = '\t' * curr_indent_level
            out += '%s}\n' % indent

        return out

    def get_egress_control_flow(self, indent_level):
        indent = '\t' * indent_level

        out = '%sif (%s == 1) {\n' % (indent, self.satisfied_meta_field)
        out += '%s\tapply(%s);\n' % (indent, self.out_header_table.get_name())
        out += '%s}\n' % indent
        return out

    def get_code(self):
        out = '// query %i\n' % self.id

        # out header
        out += self.out_header.get_header_specification_code()

        # query actions (drop, mark satisfied, add out header, etc)
        for action in self.actions.values():
            out += action.get_code()

        # query tables (add out header, mark satisfied)
        out += self.out_header_table.get_code()
        out += self.satisfied_table.get_code()

        # operator code
        for operator in self.operators:
            out += operator.get_code()
        return out

    def get_commands(self):
        commands = list()
        for operator in self.operators:
            # print str(operator)
            commands.extend(operator.get_commands())

        commands.append(self.out_header_table.get_default_command())
        commands.append(self.satisfied_table.get_default_command())

        return commands

    def get_metadata_name(self):
        return self.meta_init_name

    def get_header_format(self):
        # TODO: This will now change
        header_format = dict()
        header_format['parse_payload'] = self.parse_payload
        header_format['payload_fields'] = self.payload_fields
        header_format['reads_register'] = self.read_register
        print "%%%% get_header_format %%%% :" + str(self.out_header)
        if self.out_header:
            header_format['headers'] = self.out_header
        else:
            header_format['headers'] = None

        return header_format

    def get_update_commands(self, filter_id, update):
        commands = list()
        if filter_id in self.src_to_filter_operator:
            filter_operator = self.src_to_filter_operator[filter_id]
            filter_mask = filter_operator.get_filter_mask()
            filter_table_name = filter_operator.table.get_name()
            filter_action = filter_operator.get_match_action()

            for dip in update:
                dip = dip.strip('\n')
                commands.append('table_add %s %s  %s/%i =>' % (filter_table_name, filter_action, dip, filter_mask))
        return commands
