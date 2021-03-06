# Copyright 2012-2019 Akretion France (http://www.akretion.com)
# @author Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_is_zero
from datetime import datetime
import unicodecsv
from tempfile import TemporaryFile
import base64
import logging

logger = logging.getLogger(__name__)
try:
    import xlrd
except ImportError:
    logger.debug('Cannot import xlrd')


class AccountMoveImport(models.TransientModel):
    _name = "account.move.import"
    _description = "Import account move from CSV file"

    file_to_import = fields.Binary(
        string='File to Import', required=True,
        help="File containing the journal entry(ies) to import.")
    filename = fields.Char()
    file_format = fields.Selection([
        ('genericcsv', 'Generic CSV'),
        ('nibelis', 'Nibelis (Prisme)'),
        ('quadra', 'Quadra (without analytic)'),
        ('extenso', 'In Extenso'),
        ('cielpaye', 'Ciel Paye'),
        ('payfit', 'Payfit'),
        ('fec_txt', 'FEC (text)'),
        ], string='File Format', required=True,
        help="Select the type of file you are importing.")
    post_move = fields.Boolean(
        string='Post Journal Entry',
        help="If True, the journal entry will be posted after the import.")
    force_journal_id = fields.Many2one(
        'account.journal', string="Force Journal",
        help="Journal in which the journal entry will be created, "
        "even if the file indicate another journal.")
    force_move_ref = fields.Char('Force Reference')
    force_move_line_name = fields.Char('Force Label')
    force_move_date = fields.Date('Force Date')
    file_encoding = fields.Selection([
        ('ascii', 'ASCII'),
        ('latin1', 'ISO 8859-15 (alias Latin1)'),
        ('utf-8', 'UTF-8'),
        ], string='File Encoding', default='utf-8')
    fec_txt_field_separator = fields.Selection([
        ('pipe', '| (pipe)'),
        ('tab', 'Tabulation'),
        ], string='Field Separator', default='pipe')
    # technical fields
    force_move_date_required = fields.Boolean('Force Date Required')
    force_move_line_name_required = fields.Boolean('Force Label Required')
    force_journal_required = fields.Boolean('Force Journal Required')

    @api.onchange('file_format')
    def file_format_change(self):
        if self.file_format == 'payfit':
            self.force_move_date_required = True
            self.force_move_line_name_required = True
            self.force_journal_required = True
        else:
            self.force_move_date_required = False
            self.force_move_line_name_required = False
            self.force_journal_required = False

    # PIVOT FORMAT
    # [{
    #    'account': {'code': '411000'},
    #    'analytic': {'code': 'ADM'},
    #    'partner': {'ref': '1242'},
    #               # you can use many more keys to match partners
    #    'name': u'label',  # required
    #    'credit': 12.42,
    #    'debit': 0,
    #    'ref': '9804',  # optional
    #    'journal': {'code': 'VT'},
    #    'date': '2017-02-15',  # also accepted in datetime format
    #    'reconcile_ref': 'A1242',  # will be written in import_reconcile
    #                               # and be processed after move line creation
    #    'line': 2,  # Line number for error messages.
    #                # Must be the line number including headers
    # },
    #  2nd line...
    #  3rd line...
    # ]

    def file2pivot(self, fileobj, file_bytes):
        file_format = self.file_format
        if file_format == 'nibelis':
            return self.nibelis2pivot(fileobj)
        elif file_format == 'genericcsv':
            return self.genericcsv2pivot(fileobj)
        elif file_format == 'quadra':
            return self.quadra2pivot(file_bytes)
        elif file_format == 'extenso':
            return self.extenso2pivot(fileobj)
        elif file_format == 'payfit':
            return self.payfit2pivot(file_bytes)
        elif file_format == 'cielpaye':
            return self.cielpaye2pivot(fileobj)
        elif file_format == 'fec_txt':
            return self.fectxt2pivot(fileobj)
        else:
            raise UserError(_("You must select a file format."))

    def run_import(self):
        self.ensure_one()
        fileobj = TemporaryFile('wb+')
        file_bytes = base64.b64decode(self.file_to_import)
        fileobj.write(file_bytes)
        fileobj.seek(0)  # We must start reading from the beginning !
        pivot = self.file2pivot(fileobj, file_bytes)
        fileobj.close()
        logger.debug('pivot before update: %s', pivot)
        self.update_pivot(pivot)
        moves = self.create_moves_from_pivot(pivot, post=self.post_move)
        self.reconcile_move_lines(moves)
        action = {
            'name': _('Imported Journal Entries'),
            'res_model': 'account.move',
            'type': 'ir.actions.act_window',
            'nodestroy': False,
            'target': 'current',
            }

        if len(moves) == 1:
            action.update({
                'view_mode': 'form,tree',
                'res_id': moves[0].id,
                })
        else:
            action.update({
                'view_mode': 'tree,form',
                'domain': [('id', 'in', moves.ids)],
                })
        return action

    def update_pivot(self, pivot):
        force_move_date = self.force_move_date
        force_move_ref = self.force_move_ref
        force_move_line_name = self.force_move_line_name
        force_journal = self.force_journal_id or False
        for l in pivot:
            if force_move_date:
                l['date'] = force_move_date
            if force_move_line_name:
                l['name'] = force_move_line_name
            if force_move_ref:
                l['ref'] = force_move_ref
            if force_journal:
                l['journal'] = {'recordset': force_journal}
            if isinstance(l.get('date'), datetime):
                l['date'] = fields.Date.to_string(l['date'])
            if not l['credit']:
                l['credit'] = 0.0
            if not l['debit']:
                l['debit'] = 0.0

    def extenso2pivot(self, fileobj):
        fieldnames = [
            'journal', 'date', False, 'account', False, False, False, False,
            'debit', 'credit']
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter='\t',
            quoting=False,
            encoding='utf-8')
        res = []
        i = 0
        for l in reader:
            i += 1
            l['credit'] = l['credit'] or '0'
            l['debit'] = l['debit'] or '0'
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                'credit': float(l['credit'].replace(',', '.')),
                'debit': float(l['debit'].replace(',', '.')),
                'date': datetime.strptime(l['date'], '%d%m%Y'),
                'line': i,
            }
            res.append(vals)
        return res

    def cielpaye2pivot(self, fileobj):
        fieldnames = [
            False, 'journal', 'date', 'account', False, 'amount', 'sign',
            False, 'name', False]
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter='\t',
            quoting=unicodecsv.QUOTE_MINIMAL,
            encoding='utf-8')
        res = []
        i = 0
        for l in reader:
            i += 1
            # skip non-move lines
            if l.get('date') and l.get('name') and l.get('amount'):
                amount = float(l['amount'].replace(',', '.'))
                vals = {
                    'journal': {'code': l['journal']},
                    'account': {'code': l['account']},
                    'credit': l['sign'] == 'C' and amount or 0,
                    'debit': l['sign'] == 'D' and amount or 0,
                    'date': datetime.strptime(l['date'], '%d/%m/%Y'),
                    'name': l['name'],
                    'line': i,
                }
                res.append(vals)
        return res

    def fectxt2pivot(self, fileobj):
        fieldnames = [
            'journal', False, False, 'date', 'account', 'name',
            False, False,  # CompAuxNum|CompAuxLib
            'ref', False, 'name', 'debit', 'credit',
            'reconcile_ref', False, False, False, False]
        if self.fec_txt_field_separator == 'pipe':
            delimiter = '|'
        elif self.fec_txt_field_separator == 'tab':
            delimiter = '\t'
        else:
            raise UserError(_('You must select a field separator.'))
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=delimiter,
            encoding=self.file_encoding)
        res = []
        i = 0
        for l in reader:
            i += 1
            # Skip header line
            if i == 1:
                continue
            l['credit'] = l['credit'] or '0'
            l['debit'] = l['debit'] or '0'
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                #    'partner': {'ref': '1242'},
                'credit': float(l['credit'].replace(',', '.')),
                'debit': float(l['debit'].replace(',', '.')),
                'date': datetime.strptime(l['date'], '%Y%m%d'),
                'name': l['name'],
                'reconcile_ref': l['reconcile_ref'],
                'line': i,
            }
            res.append(vals)
        return res

    def genericcsv2pivot(self, fileobj):
        # Prisme
        fieldnames = [
            'date', 'journal', 'account', 'partner',
            'analytic', 'name', 'debit', 'credit',
            ]
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=',',
            quotechar='"',
            quoting=unicodecsv.QUOTE_MINIMAL,
            encoding='utf-8')
        res = []
        i = 0
        for l in reader:
            i += 1
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                'credit': float(l['credit'] or 0),
                'debit': float(l['debit'] or 0),
                'date': datetime.strptime(l['date'], '%d/%m/%Y'),
                'name': l['name'],
                'line': i,
                }
            if l['analytic']:
                vals['analytic'] = {'code': l['analytic']}
            if l['partner']:
                vals['partner'] = {'ref': l['partner']}
            res.append(vals)
        return res

    def nibelis2pivot(self, fileobj):
        fieldnames = [
            'trasha', 'trashb', 'journal', 'trashd', 'trashe',
            'trashf', 'trashg', 'date', 'trashi', 'trashj', 'trashk',
            'trashl', 'trashm', 'trashn', 'account', 'trashp',
            'trashq', 'amount', 'trashs', 'sign', 'trashu',
            'trashv', 'name',
            'trashx', 'trashy', 'trashz', 'trashaa', 'trashab',
            'trashac', 'trashad', 'trashae', 'analytic']
        reader = unicodecsv.DictReader(
            fileobj,
            fieldnames=fieldnames,
            delimiter=';',
            quoting=False,
            encoding='latin1')
        res = []
        i = 0
        for l in reader:
            i += 1
            if i == 1:
                continue
            amount = float(l['amount'].replace(',', '.'))
            credit = l['sign'] == 'C' and amount or False
            debit = l['sign'] == 'D' and amount or False
            ana = l.get('analytic') and {'code': l.get('analytic')} or False
            vals = {
                'journal': {'code': l['journal']},
                'account': {'code': l['account']},
                'analytic': ana,
                'credit': credit,
                'debit': debit,
                'date': datetime.strptime(l['date'], '%y%m%d'),
                'name': l['name'],
                'line': i,
            }
            res.append(vals)
        return res

    def quadra2pivot(self, file_bytes):
        i = 0
        res = []
        file_str = file_bytes.decode(self.file_encoding)
        for l in file_str.split('\n'):
            i += 1
            if len(l) < 54:
                continue
            if l[0] == 'M' and l[41] in ('C', 'D'):
                amount_cents = int(l[42:55])
                amount = amount_cents / 100.0
                vals = {
                    'journal': {'code': l[9:11]},
                    'account': {'code': l[1:9]},
                    'credit': l[41] == 'C' and amount or False,
                    'debit': l[41] == 'D' and amount or False,
                    'date': datetime.strptime(l[14:20], '%d%m%y'),
                    'name': l[21:41],
                    'line': i,
                }
                res.append(vals)
        return res

    def payfit2pivot(self, file_bytes):
        wb = xlrd.open_workbook(file_contents=file_bytes)
        sh1 = wb.sheet_by_index(1)
        i = 0
        res = []
        name = u'Paye'
        for rownum in range(sh1.nrows):
            row = sh1.row_values(rownum)
            i += 1
            if i == 1:
                continue
            if not row[0]:
                continue
            account = str(row[0])
            if '.' in account:
                account = account.split('.')[0]
            if not account[0].isdigit():
                continue
            analytic = str(row[3])
            vals = {
                'account': {'code': account},
                'name': name,
                'debit': float(row[5] or 0.0),
                'credit': float(row[6] or 0.0),
                'line': i,
            }
            if analytic:
                vals['analytic'] = {'code': analytic}
            res.append(vals)
        return res

    def create_moves_from_pivot(self, pivot, post=False):
        logger.debug('Final pivot: %s', pivot)
        bdio = self.env['business.document.import']
        amo = self.env['account.move']
        acc_speed_dict = bdio._prepare_account_speed_dict()
        aacc_speed_dict = bdio._prepare_analytic_account_speed_dict()
        journal_speed_dict = bdio._prepare_journal_speed_dict()
        chatter_msg = []
        # MATCH what needs to be matched... + CHECKS
        for l in pivot:
            assert l.get('line') and isinstance(l.get('line'), int),\
                'missing line number'
            error_prefix = _('Line %d:') % l['line']
            bdiop = bdio.with_context(error_prefix=error_prefix)
            account = bdiop._match_account(
                l['account'], chatter_msg, acc_speed_dict)
            l['account_id'] = account.id
            if l.get('partner'):
                partner = bdiop._match_partner(
                    l['partner'], chatter_msg, partner_type=False)
                l['partner_id'] = partner.commercial_partner_id.id
            if l.get('analytic'):
                analytic = bdiop._match_analytic_account(
                    l['analytic'], chatter_msg, aacc_speed_dict)
                l['analytic_account_id'] = analytic.id
            journal = bdiop._match_journal(
                l['journal'], chatter_msg, journal_speed_dict)
            l['journal_id'] = journal.id
            if not l.get('name'):
                raise UserError(_(
                    'Line %d: missing label.') % l['line'])
            if not l.get('date'):
                raise UserError(_(
                    'Line %d: missing date.') % l['line'])
            if not isinstance(l.get('credit'), float):
                raise UserError(_(
                    'Line %d: bad value for credit (%s).')
                    % (l['line'], l['credit']))
            if not isinstance(l.get('debit'), float):
                raise UserError(_(
                    'Line %d: bad value for debit (%s).')
                    % (l['line'], l['debit']))
            # test that they don't have both a value
        # EXTRACT MOVES
        moves = []
        cur_journal_id = False
        cur_ref = False
        cur_date = False
        cur_balance = 0.0
        prec = self.env.user.company_id.currency_id.rounding
        cur_move = {}
        for l in pivot:
            ref = l.get('ref', False)
            if (
                    cur_ref == ref and
                    cur_journal_id == l['journal_id'] and
                    cur_date == l['date'] and
                    not float_is_zero(cur_balance, precision_rounding=prec)):
                # append to current move
                cur_move['line_ids'].append((0, 0, self._prepare_move_line(l)))
            else:
                # new move
                if moves and not float_is_zero(
                        cur_balance, precision_rounding=prec):
                    raise UserError(_(
                        "The journal entry that ends on line %d is not "
                        "balanced (balance is %s).")
                        % (l['line'] - 1, cur_balance))
                if cur_move:
                    assert len(cur_move['line_ids']) > 1,\
                        'move should have more than 1 line'
                    moves.append(cur_move)
                cur_move = self._prepare_move(l)
                cur_move['line_ids'] = [(0, 0, self._prepare_move_line(l))]
                cur_date = l['date']
                cur_ref = ref
                cur_journal_id = l['journal_id']
            cur_balance += l['credit'] - l['debit']
        if cur_move:
            moves.append(cur_move)
        if not float_is_zero(cur_balance, precision_rounding=prec):
            raise UserError(_(
                "The journal entry that ends on the last line is not "
                "balanced (balance is %s).") % cur_balance)
        rmoves = self.env['account.move']
        for move in moves:
            rmoves += amo.create(move)
        logger.info(
            'Account moves IDs %s created via file import' % rmoves.ids)
        if post:
            rmoves.post()
        return rmoves

    def _prepare_move(self, pivot_line):
        vals = {
            'journal_id': pivot_line['journal_id'],
            'ref': pivot_line.get('ref'),
            'date': pivot_line['date'],
            }
        return vals

    def _prepare_move_line(self, pivot_line):
        vals = {
            'credit': pivot_line['credit'],
            'debit': pivot_line['debit'],
            'name': pivot_line['name'],
            'partner_id': pivot_line.get('partner_id'),
            'account_id': pivot_line['account_id'],
            'analytic_account_id': pivot_line.get('analytic_account_id'),
            'import_reconcile': pivot_line.get('reconcile_ref'),
            }
        return vals

    def reconcile_move_lines(self, moves):
        prec = self.env.user.company_id.currency_id.rounding
        logger.info('Start to reconcile imported moves')
        lines = self.env['account.move.line'].search([
            ('move_id', 'in', moves.ids),
            ('import_reconcile', '!=', False),
            ])
        torec = {}  # key = reconcile mark, value = movelines_recordset
        for line in lines:
            if line.import_reconcile in torec:
                torec[line.import_reconcile] += line
            else:
                torec[line.import_reconcile] = line
        for rec_ref, lines_to_rec in torec.items():
            if len(lines_to_rec) < 2:
                logger.warning(
                    "Skip reconcile of ref '%s' because "
                    "this ref is only on 1 move line", rec_ref)
                continue
            total = 0.0
            accounts = {}
            partners = {}
            for line in lines_to_rec:
                total += line.credit
                total -= line.debit
                accounts[line.account_id] = True
                partners[line.partner_id.id or False] = True
            if not float_is_zero(total, precision_digits=prec):
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref are not balanced (%s)", rec_ref, total)
                continue
            if len(accounts) > 1:
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref have different accounts (%s)",
                    rec_ref, ', '.join([acc.code for acc in accounts.keys()]))
                continue
            if not list(accounts)[0].reconcile:
                logger.warning(
                    "Skip reconcile of ref '%s' because the account '%s' "
                    "is not configured with 'Allow Reconciliation'",
                    rec_ref, list(accounts)[0].display_name)
                continue
            if len(partners) > 1:
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref have different partners (IDs %s)",
                    rec_ref, ', '.join(partners.keys()))
                continue
            lines_to_rec.reconcile()
        logger.info('Reconcile imported moves finished')
