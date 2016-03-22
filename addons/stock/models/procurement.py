# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from openerp.osv import fields, osv
from openerp.tools.translate import _

from openerp.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT, float_compare, float_round
from openerp import SUPERUSER_ID

from collections import defaultdict
from dateutil.relativedelta import relativedelta
from datetime import datetime
from psycopg2 import OperationalError
import openerp

class procurement_group(osv.osv):
    _inherit = 'procurement.group'
    _columns = {
        'partner_id': fields.many2one('res.partner', 'Partner')
    }

class procurement_rule(osv.osv):
    _inherit = 'procurement.rule'

    def _get_action(self, cr, uid, context=None):
        result = super(procurement_rule, self)._get_action(cr, uid, context=context)
        return result + [('move', _('Move From Another Location'))]

    def _get_rules(self, cr, uid, ids, context=None):
        res = []
        for route in self.browse(cr, uid, ids):
            res += [x.id for x in route.pull_ids]
        return res

    _columns = {
        'location_id': fields.many2one('stock.location', 'Procurement Location'),
        'location_src_id': fields.many2one('stock.location', 'Source Location',
            help="Source location is action=move"),
        'route_id': fields.many2one('stock.location.route', 'Route',
            help="If route_id is False, the rule is global"),
        'procure_method': fields.selection([('make_to_stock', 'Take From Stock'), ('make_to_order', 'Create Procurement')], 'Move Supply Method', required=True, 
                                           help="""Determines the procurement method of the stock move that will be generated: whether it will need to 'take from the available stock' in its source location or needs to ignore its stock and create a procurement over there."""),
        'route_sequence': fields.related('route_id', 'sequence', string='Route Sequence',
            store={
                'stock.location.route': (_get_rules, ['sequence'], 10),
                'procurement.rule': (lambda self, cr, uid, ids, c={}: ids, ['route_id'], 10),
        }),
        'picking_type_id': fields.many2one('stock.picking.type', 'Picking Type',
            help="Picking Type determines the way the picking should be shown in the view, reports, ..."),
        'delay': fields.integer('Number of Days'),
        'partner_address_id': fields.many2one('res.partner', 'Partner Address'),
        'propagate': fields.boolean('Propagate cancel and split', help='If checked, when the previous move of the move (which was generated by a next procurement) is cancelled or split, the move generated by this move will too'),
        'warehouse_id': fields.many2one('stock.warehouse', 'Served Warehouse', help='The warehouse this rule is for'),
        'propagate_warehouse_id': fields.many2one('stock.warehouse', 'Warehouse to Propagate', help="The warehouse to propagate on the created move/procurement, which can be different of the warehouse this rule is for (e.g for resupplying rules from another warehouse)"),
    }

    _defaults = {
        'procure_method': 'make_to_stock',
        'propagate': True,
        'delay': 0,
    }

class procurement_order(osv.osv):
    _inherit = "procurement.order"
    _columns = {
        'location_id': fields.many2one('stock.location', 'Procurement Location'),  # not required because task may create procurements that aren't linked to a location with sale_service
        'partner_dest_id': fields.many2one('res.partner', 'Customer Address', help="In case of dropshipping, we need to know the destination address more precisely"),
        'move_ids': fields.one2many('stock.move', 'procurement_id', 'Moves', help="Moves created by the procurement"),
        'move_dest_id': fields.many2one('stock.move', 'Destination Move', help="Move which caused (created) the procurement"),
        'route_ids': fields.many2many('stock.location.route', 'stock_location_route_procurement', 'procurement_id', 'route_id', 'Preferred Routes', help="Preferred route to be followed by the procurement order. Usually copied from the generating document (SO) but could be set up manually."),
        'warehouse_id': fields.many2one('stock.warehouse', 'Warehouse', help="Warehouse to consider for the route selection"),
        'orderpoint_id': fields.many2one('stock.warehouse.orderpoint', 'Minimum Stock Rule'),
    }

    def propagate_cancels(self, cr, uid, ids, context=None):
        move_cancel = []
        for procurement in self.browse(cr, uid, ids, context=context):
            if procurement.rule_id.action == 'move' and procurement.move_ids:
                move_cancel += [m.id for m in procurement.move_ids]
        if move_cancel:
            self.pool.get('stock.move').action_cancel(cr, uid, move_cancel, context=context)
        return True

    def cancel(self, cr, uid, ids, context=None):
        if context is None:
            context = {}
        to_cancel_ids = self.get_cancel_ids(cr, uid, ids, context=context)
        ctx = context.copy()
        #set the context for the propagation of the procurement cancellation
        ctx['cancel_procurement'] = True
        self.propagate_cancels(cr, uid, to_cancel_ids, context=ctx)
        return super(procurement_order, self).cancel(cr, uid, to_cancel_ids, context=ctx)

    def _find_parent_locations(self, cr, uid, ids, context=None):
        procurement = self.browse(cr, uid, ids[0], context=context)
        location = procurement.location_id
        res = [location.id]
        while location.location_id:
            location = location.location_id
            res.append(location.id)
        return res

    def change_warehouse_id(self, cr, uid, ids, warehouse_id, context=None):
        if warehouse_id:
            warehouse = self.pool.get('stock.warehouse').browse(cr, uid, warehouse_id, context=context)
            return {'value': {'location_id': warehouse.lot_stock_id.id}}
        return {}

    def _search_suitable_rule(self, cr, uid, ids, domain, context=None):
        '''we try to first find a rule among the ones defined on the procurement order group and if none is found, we try on the routes defined for the product, and finally we fallback on the default behavior'''
        procurement = self.browse(cr, uid, ids[0], context=context)
        pull_obj = self.pool.get('procurement.rule')
        warehouse_route_ids = []
        if procurement.warehouse_id:
            domain += ['|', ('warehouse_id', '=', procurement.warehouse_id.id), ('warehouse_id', '=', False)]
            warehouse_route_ids = [x.id for x in procurement.warehouse_id.route_ids]
        product_route_ids = [x.id for x in procurement.product_id.route_ids + procurement.product_id.categ_id.total_route_ids]
        procurement_route_ids = [x.id for x in procurement.route_ids]
        res = pull_obj.search(cr, uid, domain + [('route_id', 'in', procurement_route_ids)], order='route_sequence, sequence', context=context)
        if not res:
            res = pull_obj.search(cr, uid, domain + [('route_id', 'in', product_route_ids)], order='route_sequence, sequence', context=context)
            if not res:
                res = warehouse_route_ids and pull_obj.search(cr, uid, domain + [('route_id', 'in', warehouse_route_ids)], order='route_sequence, sequence', context=context) or []
                if not res:
                    res = pull_obj.search(cr, uid, domain + [('route_id', '=', False)], order='sequence', context=context)
        return res

    def _find_suitable_rule(self, cr, uid, ids, context=None):
        procurement = self.browse(cr, uid, ids[0], context=context)
        rule_id = super(procurement_order, self)._find_suitable_rule(cr, uid, ids, context=context)
        if not rule_id:
            #a rule defined on 'Stock' is suitable for a procurement in 'Stock\Bin A'
            all_parent_location_ids = procurement._find_parent_locations()
            rule_id = procurement._search_suitable_rule([('location_id', 'in', all_parent_location_ids)])
            rule_id = rule_id and rule_id[0] or False
        return rule_id

    def _run_move_create(self, cr, uid, ids, context=None):
        ''' Returns a dictionary of values that will be used to create a stock move from a procurement.
        This function assumes that the given procurement has a rule (action == 'move') set on it.

        :param procurement: browse record
        :rtype: dictionary
        '''
        procurement = self.browse(cr, uid, ids[0], context=context)
        newdate = (datetime.strptime(procurement.date_planned, '%Y-%m-%d %H:%M:%S') - relativedelta(days=procurement.rule_id.delay or 0)).strftime('%Y-%m-%d %H:%M:%S')
        group_id = False
        if procurement.rule_id.group_propagation_option == 'propagate':
            group_id = procurement.group_id and procurement.group_id.id or False
        elif procurement.rule_id.group_propagation_option == 'fixed':
            group_id = procurement.rule_id.group_id and procurement.rule_id.group_id.id or False
        #it is possible that we've already got some move done, so check for the done qty and create
        #a new move with the correct qty
        already_done_qty = 0
        for move in procurement.move_ids:
            already_done_qty += move.product_uom_qty if move.state == 'done' else 0
        qty_left = max(procurement.product_qty - already_done_qty, 0)
        vals = {
            'name': procurement.name,
            'company_id': procurement.rule_id.company_id.id or procurement.rule_id.location_src_id.company_id.id or procurement.rule_id.location_id.company_id.id or procurement.company_id.id,
            'product_id': procurement.product_id.id,
            'product_uom': procurement.product_uom.id,
            'product_uom_qty': qty_left,
            'partner_id': procurement.rule_id.partner_address_id.id or (procurement.group_id and procurement.group_id.partner_id.id) or False,
            'location_id': procurement.rule_id.location_src_id.id,
            'location_dest_id': procurement.location_id.id,
            'move_dest_id': procurement.move_dest_id and procurement.move_dest_id.id or False,
            'procurement_id': procurement.id,
            'rule_id': procurement.rule_id.id,
            'procure_method': procurement.rule_id.procure_method,
            'origin': procurement.origin,
            'picking_type_id': procurement.rule_id.picking_type_id.id,
            'group_id': group_id,
            'route_ids': [(4, x.id) for x in procurement.route_ids],
            'warehouse_id': procurement.rule_id.propagate_warehouse_id.id or procurement.rule_id.warehouse_id.id,
            'date': newdate,
            'date_expected': newdate,
            'propagate': procurement.rule_id.propagate,
            'priority': procurement.priority,
        }
        return vals

    def _run(self, cr, uid, ids, context=None):
        procurement = self.browse(cr, uid, ids[0], context=context)
        if procurement.rule_id and procurement.rule_id.action == 'move':
            if not procurement.rule_id.location_src_id:
                self.message_post(cr, uid, [procurement.id], body=_('No source location defined!'), context=context)
                return False
            move_obj = self.pool.get('stock.move')
            move_dict = procurement._run_move_create()
            #create the move as SUPERUSER because the current user may not have the rights to do it (mto product launched by a sale for example)
            move_obj.create(cr, SUPERUSER_ID, move_dict, context=context)
            return True
        return super(procurement_order, self)._run(cr, uid, ids, context=context)

    def run(self, cr, uid, ids, autocommit=False, context=None):
        new_ids = [x.id for x in self.browse(cr, uid, ids, context=context) if x.state not in ('running', 'done', 'cancel')]
        context = dict(context or {}, procurement_auto_defer=True) #When creating
        res = super(procurement_order, self).run(cr, uid, new_ids, autocommit=autocommit, context=context)

        #after all the procurements are run, check if some created a draft stock move that needs to be confirmed
        #(we do that in batch because it fasts the picking assignation and the picking state computation)
        move_to_confirm_ids = []
        for procurement in self.browse(cr, uid, new_ids, context=context):
            if procurement.state == "running" and procurement.rule_id and procurement.rule_id.action == "move":
                move_to_confirm_ids += [m.id for m in procurement.move_ids if m.state == 'draft']
        if move_to_confirm_ids:
            self.pool.get('stock.move').action_confirm(cr, uid, move_to_confirm_ids, context=context)
        # If procurements created other procurements, run the created in batch
        procurement_ids = self.search(cr, uid, [('move_dest_id.procurement_id', 'in', new_ids)], order='id', context=context)
        if procurement_ids:
            res = res and self.run(cr, uid, procurement_ids, autocommit=autocommit, context=context)
        return res

    def _check(self, cr, uid, ids, context=None):
        ''' Implement the procurement checking for rules of type 'move'. The procurement will be satisfied only if all related
            moves are done/cancel and if the requested quantity is moved.
        '''
        procurement = self.browse(cr, uid, ids[0], context=context)
        if procurement.rule_id and procurement.rule_id.action == 'move':
            uom_obj = self.pool.get('product.uom')
            # In case Phantom BoM splits only into procurements
            if not procurement.move_ids:
                return True
            cancel_test_list = [x.state == 'cancel' for x in procurement.move_ids]
            done_cancel_test_list = [x.state in ('done', 'cancel') for x in procurement.move_ids]
            at_least_one_cancel = any(cancel_test_list)
            all_done_or_cancel = all(done_cancel_test_list)
            all_cancel = all(cancel_test_list)
            if not all_done_or_cancel:
                return False
            elif all_done_or_cancel and not all_cancel:
                return True
            elif all_cancel:
                self.message_post(cr, uid, [procurement.id], body=_('All stock moves have been cancelled for this procurement.'), context=context)
            self.write(cr, uid, [procurement.id], {'state': 'cancel'}, context=context)
            return False

        return super(procurement_order, self)._check(cr, uid, procurement, context)

    def do_view_pickings(self, cr, uid, ids, context=None):
        '''
        This function returns an action that display the pickings of the procurements belonging
        to the same procurement group of given ids.
        '''
        mod_obj = self.pool.get('ir.model.data')
        act_obj = self.pool.get('ir.actions.act_window')
        result = mod_obj.get_object_reference(cr, uid, 'stock', 'do_view_pickings')
        id = result and result[1] or False
        result = act_obj.read(cr, uid, [id], context=context)[0]
        group_ids = set([proc.group_id.id for proc in self.browse(cr, uid, ids, context=context) if proc.group_id])
        result['domain'] = "[('group_id','in',[" + ','.join(map(str, list(group_ids))) + "])]"
        return result

    def run_scheduler(self, cr, uid, use_new_cursor=False, company_id=False, context=None):
        '''
        Call the scheduler in order to check the running procurements (super method), to check the minimum stock rules
        and the availability of moves. This function is intended to be run for all the companies at the same time, so
        we run functions as SUPERUSER to avoid intercompanies and access rights issues.

        @param self: The object pointer
        @param cr: The current row, from the database cursor,
        @param uid: The current user ID for security checks
        @param ids: List of selected IDs
        @param use_new_cursor: if set, use a dedicated cursor and auto-commit after processing each procurement.
            This is appropriate for batch jobs only.
        @param context: A standard dictionary for contextual values
        @return:  Dictionary of values
        '''
        super(procurement_order, self).run_scheduler(cr, uid, use_new_cursor=use_new_cursor, company_id=company_id, context=context)
        if context is None:
            context = {}
        try:
            if use_new_cursor:
                cr = openerp.registry(cr.dbname).cursor()

            move_obj = self.pool.get('stock.move')

            #Minimum stock rules
            self._procure_orderpoint_confirm(cr, SUPERUSER_ID, use_new_cursor=use_new_cursor, company_id=company_id, context=context)

            #Search all confirmed stock_moves and try to assign them
            confirmed_ids = move_obj.search(cr, uid, [('state', '=', 'confirmed')], limit=None, order='priority desc, date_expected asc', context=context)
            for x in xrange(0, len(confirmed_ids), 100):
                move_obj.action_assign(cr, uid, confirmed_ids[x:x + 100], context=context)
                if use_new_cursor:
                    cr.commit()

            if use_new_cursor:
                cr.commit()
        finally:
            if use_new_cursor:
                try:
                    cr.close()
                except Exception:
                    pass
        return {}

    def _procurement_from_orderpoint_get_order(self, cr, uid, context=None):
        return 'location_id'

    def _procurement_from_orderpoint_get_grouping_key(self, cr, uid, orderpoint_ids, context=None):
        orderpoint = self.pool['stock.warehouse.orderpoint'].browse(cr, uid, orderpoint_ids[0], context=context)
        return orderpoint.location_id.id

    def _procurement_from_orderpoint_get_groups(self, cr, uid, orderpoint_ids, context=None):
        """ Make groups for a given orderpoint; by default schedule all operations in one without date """
        return [{'to_date': False, 'procurement_values': dict()}]

    def _procurement_from_orderpoint_post_process(self, cr, uid, orderpoint_ids, context=None):
        return True

    def _procure_orderpoint_confirm(self, cr, uid, use_new_cursor=False, company_id=False, context=None):
        '''
        Create procurement based on Orderpoint

        :param bool use_new_cursor: if set, use a dedicated cursor and auto-commit after processing each procurement.
            This is appropriate for batch jobs only.
        '''
        if context is None:
            context = {}
        procurement_context = dict(context, procurement_autorun_defer=True)
        if use_new_cursor:
            cr = openerp.registry(cr.dbname).cursor()
        orderpoint_obj = self.pool.get('stock.warehouse.orderpoint')
        procurement_obj = self.pool.get('procurement.order')
        product_obj = self.pool.get('product.product')

        orderpoint_ids = orderpoint_obj.search(
            cr, uid,
            company_id and [('company_id', '=', company_id)] or [],
            order=self._procurement_from_orderpoint_get_order(cr, uid, context=context))
        procurement_ids = []
        while orderpoint_ids:
            current_ids = orderpoint_ids[:1000]
            orderpoint_ids = orderpoint_ids[1000:]
            orderpoints = orderpoint_obj.browse(cr, uid, current_ids, context=context)

            # Calculate groups that can be executed together
            location_data = defaultdict(lambda: dict(products=list(), orderpoints=list(), groups=list()))

            for orderpoint in orderpoints:
                key = self._procurement_from_orderpoint_get_grouping_key(cr, uid, [orderpoint.id], context=context)
                location_data[key]['products'] += orderpoint.product_id
                location_data[key]['orderpoints'] += orderpoint
                location_data[key]['groups'] = self._procurement_from_orderpoint_get_groups(cr, uid, [orderpoint.id], context=context)

            for location_id, location_data in location_data.iteritems():
                location_orderpoints = location_data['orderpoints']
                product_context = dict(context, location=location_orderpoints[0].location_id.id)
                substract_quantity = self.pool['stock.warehouse.orderpoint'].subtract_procurements_from_orderpoints(
                    cr, uid,
                    [op.id for op in location_orderpoints],
                    context=context)

                for group in location_data['groups']:
                    if group['to_date']:
                        product_context['to_date'] = group['to_date'].strftime(DEFAULT_SERVER_DATETIME_FORMAT)
                    product_quantity = product_obj._product_available(cr, uid, [prod.id for prod in location_data['products']], context=product_context)
                    for orderpoint in location_orderpoints:
                        try:
                            op_product_virtual = product_quantity[orderpoint.product_id.id]['virtual_available']
                            if op_product_virtual is None:
                                continue
                            if float_compare(op_product_virtual, orderpoint.product_min_qty, precision_rounding=orderpoint.product_uom.rounding) <= 0:
                                qty = max(orderpoint.product_min_qty, orderpoint.product_max_qty) - op_product_virtual
                                remainder = orderpoint.qty_multiple > 0 and qty % orderpoint.qty_multiple or 0.0

                                if float_compare(remainder, 0.0, precision_rounding=orderpoint.product_uom.rounding) > 0:
                                    qty += orderpoint.qty_multiple - remainder

                                if float_compare(qty, 0.0, precision_rounding=orderpoint.product_uom.rounding) < 0:
                                    continue

                                qty -= substract_quantity[orderpoint.id]
                                qty_rounded = float_round(qty, precision_rounding=orderpoint.product_uom.rounding)
                                if qty_rounded > 0:
                                    new_procurement_id = procurement_obj.create(
                                        cr, uid,
                                        orderpoint._prepare_procurement_values(qty_rounded, **group['procurement_values']),
                                        context=procurement_context)
                                    procurement_ids.append(new_procurement_id)
                                    self._procurement_from_orderpoint_post_process(cr, uid, [orderpoint.id], context=context)
                                if use_new_cursor:
                                    cr.commit()

                        except OperationalError:
                            if use_new_cursor:
                                orderpoint_ids += orderpoint.id
                                cr.rollback()
                                continue
                            else:
                                raise

            try:
                procurement_ids.reverse()
                self.run(cr, uid, procurement_ids, context=context)
                procurement_ids = []
                if use_new_cursor:
                    cr.commit()
            except OperationalError:
                if use_new_cursor:
                    cr.rollback()
                    continue
                else:
                    raise

            if use_new_cursor:
                cr.commit()

        if use_new_cursor:
            cr.commit()
            cr.close()
        return {}