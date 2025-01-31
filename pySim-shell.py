#!/usr/bin/env python3

# Interactive shell for working with SIM / UICC / USIM / ISIM cards
#
# (C) 2021 by Harald Welte <laforge@osmocom.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from typing import List

import json

import cmd2
from cmd2 import style, fg, bg
from cmd2 import CommandSet, with_default_category, with_argparser
import argparse

import os
import sys
from optparse import OptionParser

from pySim.ts_51_011 import EF, DF, EF_SST_map, EF_AD_mode_map
from pySim.ts_31_102 import EF_UST_map, EF_USIM_ADF_map
from pySim.ts_31_103 import EF_IST_map, EF_ISIM_ADF_map

from pySim.exceptions import *
from pySim.commands import SimCardCommands
from pySim.cards import card_detect, Card
from pySim.utils import h2b, swap_nibbles, rpad, h2s
from pySim.utils import dec_st, init_reader, sanitize_pin_adm, tabulate_str_list
from pySim.card_handler import card_handler

from pySim.filesystem import CardMF, RuntimeState, CardDF, CardADF
from pySim.ts_51_011 import CardProfileSIM, DF_TELECOM, DF_GSM
from pySim.ts_102_221 import CardProfileUICC
from pySim.ts_31_102 import ADF_USIM
from pySim.ts_31_103 import ADF_ISIM

class PysimApp(cmd2.Cmd):
	CUSTOM_CATEGORY = 'pySim Commands'
	def __init__(self, card, rs):
		basic_commands = [Iso7816Commands(), UsimCommands()]
		super().__init__(persistent_history_file='~/.pysim_shell_history', allow_cli_args=False,
				use_ipython=True, auto_load_commands=False, command_sets=basic_commands)
		self.intro = style('Welcome to pySim-shell!', fg=fg.red)
		self.default_category = 'pySim-shell built-in commands'
		self.card = card
		self.rs = rs
		self.py_locals = { 'card': self.card, 'rs' : self.rs }
		self.card.read_aids()
		self.poutput('AIDs on card: %s' % (self.card._aids))
		self.numeric_path = False
		self.add_settable(cmd2.Settable('numeric_path', bool, 'Print File IDs instead of names',
						  onchange_cb=self._onchange_numeric_path))
		self.update_prompt()

	def _onchange_numeric_path(self, param_name, old, new):
		self.update_prompt()

	def update_prompt(self):
		path_list = self.rs.selected_file.fully_qualified_path(not self.numeric_path)
		self.prompt = 'pySIM-shell (%s)> ' % ('/'.join(path_list))

	@cmd2.with_category(CUSTOM_CATEGORY)
	def do_intro(self, _):
		"""Display the intro banner"""
		self.poutput(self.intro)

	@cmd2.with_category(CUSTOM_CATEGORY)
	def do_verify_adm(self, arg):
		"""VERIFY the ADM1 PIN"""
		pin_adm = sanitize_pin_adm(arg)
		self.card.verify_adm(h2b(pin_adm))



@with_default_category('ISO7816 Commands')
class Iso7816Commands(CommandSet):
	def __init__(self):
		super().__init__()

	def do_select(self, opts):
		"""SELECT a File (ADF/DF/EF)"""
		path = opts.arg_list[0]
		fcp_dec = self._cmd.rs.select(path, self._cmd)
		self._cmd.update_prompt()
		self._cmd.poutput(json.dumps(fcp_dec, indent=4))

	def complete_select(self, text, line, begidx, endidx) -> List[str]:
		"""Command Line tab completion for SELECT"""
		index_dict = { 1: self._cmd.rs.selected_file.get_selectable_names() }
		return self._cmd.index_based_complete(text, line, begidx, endidx, index_dict=index_dict)

	verify_chv_parser = argparse.ArgumentParser()
	verify_chv_parser.add_argument('--chv-nr', type=int, default=1, help='CHV Number')
	verify_chv_parser.add_argument('code', help='CODE/PIN/PUK')

	@cmd2.with_argparser(verify_chv_parser)
	def do_verify_chv(self, opts):
		"""Verify (authenticate) using specified CHV (PIN)"""
		(data, sw) = self._cmd.card._scc.verify_chv(opts.chv_nr, opts.code)
		self._cmd.poutput(data)

	dir_parser = argparse.ArgumentParser()
	dir_parser.add_argument('--fids', help='Show file identifiers', action='store_true')
	dir_parser.add_argument('--names', help='Show file names', action='store_true')
	dir_parser.add_argument('--apps', help='Show applications', action='store_true')
	dir_parser.add_argument('--all', help='Show all selectable identifiers and names', action='store_true')

	@cmd2.with_argparser(dir_parser)
	def do_dir(self, opts):
		"""Show a listing of files available in currently selected DF or MF"""
		if opts.all:
			flags = []
		elif opts.fids or opts.names or opts.apps:
			flags = ['PARENT', 'SELF']
			if opts.fids:
				flags += ['FIDS', 'AIDS']
			if opts.names:
				flags += ['FNAMES', 'ANAMES']
			if opts.apps:
				flags += ['ANAMES', 'AIDS']
		else:
			flags = ['PARENT', 'SELF', 'FNAMES', 'ANAMES']
		selectables = list(self._cmd.rs.selected_file.get_selectable_names(flags = flags))
		directory_str = tabulate_str_list(selectables, width = 79, hspace = 2, lspace = 1, align_left = True)
		path_list = self._cmd.rs.selected_file.fully_qualified_path(True)
		self._cmd.poutput('/'.join(path_list))
		path_list = self._cmd.rs.selected_file.fully_qualified_path(False)
		self._cmd.poutput('/'.join(path_list))
		self._cmd.poutput(directory_str)
		self._cmd.poutput("%d files" % len(selectables))

	def walk(self, indent = 0, action = None, context = None):
		"""Recursively walk through the file system, starting at the currently selected DF"""
		files = self._cmd.rs.selected_file.get_selectables(flags = ['FNAMES', 'ANAMES'])
		for f in files:
			if not action:
				output_str = "  " * indent + str(f) + (" " * 250)
				output_str = output_str[0:25]
				if isinstance(files[f], CardADF):
					output_str += " " + str(files[f].aid)
				else:
					output_str += " " + str(files[f].fid)
				output_str += " " + str(files[f].desc)
				self._cmd.poutput(output_str)
			if isinstance(files[f], CardDF):
				fcp_dec = self._cmd.rs.select(f, self._cmd)
				self.walk(indent + 1, action, context)
				fcp_dec = self._cmd.rs.select("..", self._cmd)
			elif action:
				action(f, context)

	def do_tree(self, opts):
		"""Display a filesystem-tree with all selectable files"""
		self.walk()



@with_default_category('USIM Commands')
class UsimCommands(CommandSet):
	def __init__(self):
		super().__init__()

	def do_read_ust(self, _):
		"""Read + Display the EF.UST"""
		self._cmd.card.select_adf_by_aid(adf="usim")
		(res, sw) = self._cmd.card.read_ust()
		self._cmd.poutput(res[0])
		self._cmd.poutput(res[1])

	def do_read_ehplmn(self, _):
		"""Read EF.EHPLMN"""
		self._cmd.card.select_adf_by_aid(adf="usim")
		(res, sw) = self._cmd.card.read_ehplmn()
		self._cmd.poutput(res)

def parse_options():

	parser = OptionParser(usage="usage: %prog [options]")

	parser.add_option("-d", "--device", dest="device", metavar="DEV",
			help="Serial Device for SIM access [default: %default]",
			default="/dev/ttyUSB0",
		)
	parser.add_option("-b", "--baud", dest="baudrate", type="int", metavar="BAUD",
			help="Baudrate used for SIM access [default: %default]",
			default=9600,
		)
	parser.add_option("-p", "--pcsc-device", dest="pcsc_dev", type='int', metavar="PCSC",
			help="Which PC/SC reader number for SIM access",
			default=None,
		)
	parser.add_option("--modem-device", dest="modem_dev", metavar="DEV",
			help="Serial port of modem for Generic SIM Access (3GPP TS 27.007)",
			default=None,
		)
	parser.add_option("--modem-baud", dest="modem_baud", type="int", metavar="BAUD",
			help="Baudrate used for modem's port [default: %default]",
			default=115200,
		)
	parser.add_option("--osmocon", dest="osmocon_sock", metavar="PATH",
			help="Socket path for Calypso (e.g. Motorola C1XX) based reader (via OsmocomBB)",
			default=None,
		)

	parser.add_option("-a", "--pin-adm", dest="pin_adm",
			help="ADM PIN used for provisioning (overwrites default)",
		)
	parser.add_option("-A", "--pin-adm-hex", dest="pin_adm_hex",
			help="ADM PIN used for provisioning, as hex string (16 characters long",
		)

	(options, args) = parser.parse_args()

	if args:
		parser.error("Extraneous arguments")

	return options



if __name__ == '__main__':

	# Parse options
	opts = parse_options()

	# Init card reader driver
	sl = init_reader(opts)
	if (sl == None):
		exit(1)

	# Create command layer
	scc = SimCardCommands(transport=sl)

	sl.wait_for_card();

	card_handler = card_handler(sl)

	card = card_detect("auto", scc)
	if card is None:
		print("No card detected!")
		sys.exit(2)

	profile = CardProfileUICC()
	rs = RuntimeState(card, profile)

	# FIXME: do this dynamically
	rs.mf.add_file(DF_TELECOM())
	rs.mf.add_file(DF_GSM())
	rs.mf.add_application(ADF_USIM())
	rs.mf.add_application(ADF_ISIM())

	app = PysimApp(card, rs)
	app.cmdloop()
