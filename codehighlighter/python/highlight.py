# Code Highligher 2 is a LibreOffice extension to highlight code snippets
# over 350 languages.

# Copyright (C) 2017  Gobinath, 2022 jmzambon

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# python standard
import re
import traceback
from math import log10

# pygments
import pygments
from pygments import styles
from pygments.lexers import get_all_lexers, get_lexer_by_name, guess_lexer
from pygments.styles import get_all_styles

# uno
import uno, unohelper
from com.sun.star.awt import Selection, XDialogEventHandler
from com.sun.star.awt.FontSlant import NONE as SL_NONE, ITALIC as SL_ITALIC
from com.sun.star.awt.FontWeight import NORMAL as W_NORMAL, BOLD as W_BOLD
from com.sun.star.awt.MessageBoxType import ERRORBOX
from com.sun.star.beans import PropertyValue
from com.sun.star.document import XUndoAction
from com.sun.star.drawing.FillStyle import NONE as FS_NONE, SOLID as FS_SOLID
from com.sun.star.lang import Locale
from com.sun.star.sheet.CellFlags import STRING as CF_STRING
from com.sun.star.task import XJobExecutor
from com.sun.star.uno import RuntimeException

# internal
import ch2_i18n

# prepare logger
import os.path
import logging
LOGLEVEL = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
logger = logging.getLogger("codehighlighter")
formatter = logging.Formatter("%(levelname)s [%(funcName)s::%(lineno)d] %(message)s")
consolehandler = logging.StreamHandler()
consolehandler.setFormatter(formatter)
try:
    userpath = uno.getComponentContext().ServiceManager.createInstance(
                    "com.sun.star.util.PathSubstitution").substituteVariables("$(user)", True)
    logfile = os.path.join(uno.fileUrlToSystemPath(userpath), "codehighlighter.log")
    filehandler = logging.FileHandler(logfile, mode="w", delay=True)
    filehandler.setFormatter(formatter)
except RuntimeException:
    # At installation time, no context is available -> just ignore it.
    pass


class UndoAction(unohelper.Base, XUndoAction):
    '''
    Add undo/redo action for highlighting operations not catched by the system,
    i.e. when applied on textbox objects.
    '''

    def __init__(self, doc, textbox, title):
        self.doc = doc
        self.textbox = textbox
        self.old_portions = None
        self.old_bg = None
        self.new_portions = None
        self.new_bg = None
        self.charprops = ("CharColor", "CharLocale", "CharPosture", "CharHeight", "CharWeight")
        self.bgprops = ("FillColor", "FillStyle")
        self.get_old_state()
        # XUndoAction attribute
        self.Title = title

    # XUndoAction (https://www.openoffice.org/api/docs/common/ref/com/sun/star/document/XUndoAction.html)
    def undo(self):
        self.textbox.setString(self.old_text)
        self._format(self.old_portions, self.old_bg)

    def redo(self):
        self.textbox.setString(self.new_text)
        self._format(self.new_portions, self.new_bg)

    # public
    def get_old_state(self):
        '''
        Gather text formattings before code highlighting.
        Will be used by <undo> to restore old state.
        '''

        self.old_bg = self.textbox.getPropertyValues(self.bgprops)
        self.old_text = self.textbox.String
        self.old_portions = self._extract_portions()

    def get_new_state(self):
        '''
        Gather text formattings after code highlighting.
        Will be used by <redo> to apply new state again.
        '''

        self.new_bg = self.textbox.getPropertyValues(self.bgprops)
        self.new_text = self.textbox.String
        self.new_portions = self._extract_portions()

    # private
    def _extract_portions(self):
        textportions = []
        for para in self.textbox:
            if textportions:    # new paragraph after first one
                textportions[-1][0] += 1
            for portion in para:
                plen = len(portion.String)
                pprops = portion.getPropertyValues(self.charprops)
                if textportions and textportions[-1][1] == pprops:
                    textportions[-1][0] += plen
                else:
                    textportions.append([plen, pprops])
        return textportions

    def _format(self, portions, bg):
        self.textbox.setPropertyValues(self.bgprops, bg)
        cursor = self.textbox.createTextCursor()
        cursor.gotoStart(False)
        for length, props in portions:
            cursor.goRight(length, True)
            cursor.setPropertyValues(self.charprops, props)
            cursor.collapseToEnd()
        self.doc.CurrentController.select(self.textbox)
        self.doc.setModified(True)


class CodeHighlighter(unohelper.Base, XJobExecutor, XDialogEventHandler):
    def __init__(self, ctx):
        try:
            self.ctx = ctx
            self.sm = ctx.ServiceManager
            self.desktop = self.create("com.sun.star.frame.Desktop")
            self.doc = self.desktop.getCurrentComponent()
            self.cfg_access = self.create_cfg_access()
            self.options = self.load_options()
            self.setlogger()
            logger.debug(f"Code Highlihter started from {self.doc.Title}.")
            logger.info(f"Using Pygments version {pygments.__version__}.")
            logger.info(f"Loaded options = {self.options}.")
            self.frame = self.doc.CurrentController.Frame
            self.dispatcher = self.create("com.sun.star.frame.DispatchHelper")
            self.strings = ch2_i18n.getstrings(ctx)
            self.nolocale = Locale("zxx", "", "")
        except Exception:
            logger.exception("")

    # XJobExecutor (https://www.openoffice.org/api/docs/common/ref/com/sun/star/task/XJobExecutor.html)
    def trigger(self, arg):
        logger.debug(f"Code Highlighter triggered with argument '{arg}'.")
        try:
            getattr(self, 'do_'+arg)()
        except Exception:
            logger.exception("")

    # XDialogEventHandler (http://www.openoffice.org/api/docs/common/ref/com/sun/star/awt/XDialogEventHandler.html)
    def callHandlerMethod(self, dialog, event, method):
        logger.debug(f"Dialog handler action: '{method}'.")
        if method == "topage1":
            dialog.Model.Step = 1
            dialog.getControl('cb_lang').setFocus()
            return True
        elif method == "topage2":
            dialog.Model.Step = 2
            dialog.getControl('nb_start').setFocus()
            return True
        return False

    def getSupportedMethodNames(self):
        return 'topage1', 'topage2'

    # main functions
    def do_highlight(self):
        '''Open option dialog and start code highlighting.'''

        if self.choose_options():
            self.prepare_highlight()

    def do_highlight_previous(self):
        '''Start code highlighting with current options as default.'''

        self.prepare_highlight()

    # private functions
    def create(self, service):
        '''Instanciate UNO services'''

        return self.sm.createInstance(service)

    def msgbox(self, message, boxtype=ERRORBOX, title="Error"):
        '''Simple UNO message box for notifications at user.'''

        win = self.frame.ContainerWindow
        box = win.Toolkit.createMessageBox(win, boxtype, 1, title, message)
        return box.execute()

    def to_int(self, hex_str):
        '''Convert hexadecimal color representation into decimal integer.'''

        if hex_str:
            return int(hex_str[-6:], 16)
        return 0

    def setlogger(self):
        loglevel = LOGLEVEL.get(self.options["LogLevel"], 0)
        logger.setLevel(loglevel)
        if self.options["LogToFile"] == 0:
            logger.removeHandler(filehandler)
            logger.addHandler(consolehandler)
        else:
            logger.removeHandler(consolehandler)
            logger.addHandler(filehandler)

    def create_cfg_access(self):
        '''Return an updatable instance of the codehighlighter node in LO registry. '''

        cfg = self.create('com.sun.star.configuration.ConfigurationProvider')
        prop = PropertyValue('nodepath', 0, '/ooo.ext.code-highlighter.Registry/Settings', 0)
        cfg_access = cfg.createInstanceWithArguments('com.sun.star.configuration.ConfigurationUpdateAccess', (prop,))
        return cfg_access

    def load_options(self):
        properties = self.cfg_access.ElementNames
        values = self.cfg_access.getPropertyValues(properties)
        return dict(zip(properties, values))

    def create_dialog(self):
        '''Load, populate and return options dialog.'''

        # get_all_lexers() returns: (longname, tuple of aliases, tuple of filename patterns, tuple of mimetypes)
        logger.debug("Starting options dialog.")
        all_lexers = sorted((lex[0] for lex in get_all_lexers()), key=str.casefold)
        self.all_lexer_aliases = [lex[0].lower() for lex in get_all_lexers()]
        for lex in get_all_lexers():
            self.all_lexer_aliases.extend(list(lex[1]))
        logger.debug("--> getting lexers ok.")
        self.all_styles = sorted(get_all_styles(), key=lambda x: (x != 'default', x.lower()))
        logger.debug("--> getting styles ok.")

        dialog_provider = self.create("com.sun.star.awt.DialogProvider2")
        dialog = dialog_provider.createDialogWithHandler(
            "vnd.sun.star.extension://javahelps.codehighlighter/dialogs/CodeHighlighter2.xdl", self)
        logger.debug("--> creating dialog ok.")

        # set localized strings
        controlnames = ("label_lang", "label_style", "check_col_bg", "check_linenb", "nb_line", "lbl_nb_start",
                        "lbl_nb_ratio", "lbl_nb_sep", "pygments_ver", "topage1", "topage2")
        for controlname in controlnames:
            dialog.getControl(controlname).Model.setPropertyValues(("Label", "HelpText"), self.strings[controlname])
        # dialog.getControl("nb_sep").Model.HelpText = self.strings["nb_sep"][1]

        cb_lang = dialog.getControl('cb_lang')
        cb_style = dialog.getControl('cb_style')
        check_col_bg = dialog.getControl('check_col_bg')
        check_linenb = dialog.getControl('check_linenb')
        nb_start = dialog.getControl('nb_start')
        nb_ratio = dialog.getControl('nb_ratio')
        nb_sep = dialog.getControl('nb_sep')
        pygments_ver = dialog.getControl('pygments_ver')

        cb_lang.Text = self.options['Language']
        cb_lang.setSelection(Selection(0, len(cb_lang.Text)))
        cb_lang.addItems(all_lexers, 0)
        cb_lang.addItem('automatic', 0)

        style = self.options['Style']
        if style in self.all_styles:
            cb_style.Text = style
        cb_style.addItems(self.all_styles, 0)

        check_col_bg.State = self.options['ColourizeBackground']
        check_linenb.State = self.options['ShowLineNumbers']
        nb_start.Value = self.options['LineNumberStart']
        nb_ratio.Value = self.options['LineNumberRatio']
        nb_sep.Text = self.options['LineNumberSeparator']
        logger.debug("--> filling controls ok.")

        def getextver():
            pip = self.ctx.getByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
            extensions = pip.getExtensionList()
            for e in extensions:
                if "javahelps.codehighlighter" in e:
                    return e[1]
            return ''
        dialog.Title = dialog.Title.format(getextver())
        pygments_ver.Text = pygments_ver.Text.format(pygments.__version__)
        logger.debug("Dialog returned.")

        return dialog

    def choose_options(self):
        '''
        Get options choice.
        Dialog return values: 0 = Canceled, 1 = OK
        '''

        # dialog.setVisible(True)
        dialog = self.create_dialog()
        if dialog.execute() == 0:
            logger.debug("Dialog canceled.")
            return False
        lang = dialog.getControl('cb_lang').Text.strip() or 'automatic'
        style = dialog.getControl('cb_style').Text.strip() or 'default'
        colorize_bg = dialog.getControl('check_col_bg').State
        show_linenb = dialog.getControl('check_linenb').State
        nb_start = int(dialog.getControl('nb_start').Value)
        nb_ratio = int(dialog.getControl('nb_ratio').Value)
        nb_sep = dialog.getControl('nb_sep').Text

        if lang != 'automatic' and lang.lower() not in self.all_lexer_aliases:
            self.msgbox(self.strings["errlang"])
            return False
        if style not in self.all_styles:
            self.msgbox(self.strings["errstyle"])
            return False
        self.save_options(Style=style, Language=lang, ColourizeBackground=colorize_bg, ShowLineNumbers=show_linenb,
                          LineNumberStart=nb_start, LineNumberRatio=nb_ratio, LineNumberSeparator=nb_sep)
        logger.debug("Dialog validated and options saved.")
        logger.info(f"Updated options = {self.options}.")
        return True

    def save_options(self, **kwargs):
        self.options.update(kwargs)
        self.cfg_access.setPropertyValues(tuple(kwargs.keys()), tuple(kwargs.values()))
        self.cfg_access.commitChanges()

    def getlexer(self, code):
        lang = self.options['Language']
        if lang == 'automatic':
            lexer = guess_lexer(code)
            logger.info(f'Automatic lexer choice : {lexer.name}')
        else:
            try:
                lexer = get_lexer_by_name(lang)
            except pygments.util.ClassNotFound:
                # get_lexer_by_name() only checks aliases, not the actual longname
                for lex in get_all_lexers():
                    if lex[0].lower() == lang.lower():
                        # found the longname, use the first alias
                        lexer = get_lexer_by_name(lex[1][0])
                        break
                else:
                    raise
        # prevent offset color if selection start with empty line
        lexer.stripnl = False
        return lexer

    def prepare_highlight(self, selected_item=None):
        '''
        Check if selection is valid and contains text.
        If there is no selection but cursor is inside a text frame or
        a text table cell, and that this frame or cell contains text,
        selection is extended to the whole container.
        If cursor is inside a text shape or a Calc cell, selection is extended
        to the whole container in any case.
        If selection contains only part of paragraphs, selection is
        extended to the entire paragraphs.
        '''

        stylename = self.options['Style']
        style = styles.get_style_by_name(stylename)
        bg_color = style.background_color if self.options['ColourizeBackground'] else None

        if not self.doc.hasControllersLocked():
            self.doc.lockControllers()
            logger.debug("Controllers locked.")
        undomanager = self.doc.UndoManager
        hascode = False
        try:
            # Get the selected item
            if selected_item is None:
                selected_item = self.doc.CurrentSelection

            if not hasattr(selected_item, 'supportsService'):
                self.msgbox(self.strings["errsel1"])
                logger.debug("Invalid selection (1)")
                return

            # TEXT SHAPES
            elif selected_item.ImplementationName == "com.sun.star.drawing.SvxShapeCollection":
                logger.debug("Dealing with text shapes.")
                for code_block in selected_item:
                    code = code_block.String
                    if code.strip():
                        hascode = True
                        lexer = self.getlexer(code)
                        # exit edit mode if necessary
                        self.dispatcher.executeDispatch(self.frame, ".uno:SelectObject", "", 0, ())
                        undoaction = UndoAction(self.doc, code_block,
                                                f"code highlight (lang: {lexer.name}, style: {stylename})")
                        logger.debug("Custom undo action created.")
                        if self.show_line_numbers(code_block):
                            code = code_block.String    # code string has changed
                        cursor = code_block.createTextCursorByRange(code_block)
                        cursor.CharLocale = self.nolocale
                        cursor.collapseToStart()
                        self.highlight_code(code, cursor, lexer, style)
                        # unlock controllers here to force left pane syncing in draw/impress
                        if self.doc.supportsService("com.sun.star.drawing.GenericDrawingDocument"):
                            self.doc.unlockControllers()
                            logger.debug("Controllers unlocked.")
                        code_block.FillStyle = FS_NONE
                        if bg_color:
                            code_block.FillStyle = FS_SOLID
                            code_block.FillColor = self.to_int(bg_color)
                        # model is not considered as modified after textbox formatting
                        self.doc.setModified(True)
                        undoaction.get_new_state()
                        undomanager.addUndoAction(undoaction)
                        logger.debug("Custom undo action added.")

            # PLAIN TEXTS
            elif selected_item.ImplementationName == "SwXTextRanges":
                logger.debug("Dealing with text ranges.")
                for code_block in selected_item:
                    code = code_block.String
                    if code.strip():
                        hascode = True
                        lexer = self.getlexer(code)
                        try:
                            undomanager.enterUndoContext(f"code highlight (lang: {lexer.name}, style: {stylename})")
                            self.show_line_numbers(code_block, isplaintext=True)
                            cursor, code = self.ensure_paragraphs(code_block)
                            cursor.ParaBackColor = -1
                            if bg_color:
                                cursor.ParaBackColor = self.to_int(bg_color)
                            cursor.CharLocale = self.nolocale
                            self.doc.CurrentController.select(cursor)
                            cursor.collapseToStart()
                            self.highlight_code(code, cursor, lexer, style)
                        finally:
                            undomanager.leaveUndoContext()

                if not hascode and selected_item.Count == 1:
                    code_block = selected_item[0]
                    if code_block.TextFrame:
                        self.prepare_highlight(code_block.TextFrame)
                        return
                    elif code_block.TextTable:
                        cellname = code_block.Cell.CellName
                        texttablecursor = code_block.TextTable.createCursorByCellName(cellname)
                        self.prepare_highlight(texttablecursor)
                        return

            # TEXT FRAME
            elif selected_item.ImplementationName == "SwXTextFrame":
                logger.debug("Dealing with a text frame")
                code_block = selected_item
                code = code_block.String
                if code.strip():
                    hascode = True
                    lexer = self.getlexer(code)
                    undomanager.enterUndoContext(f"code highlight (lang: {lexer.name}, style: {stylename})")
                    if self.show_line_numbers(code_block):
                        code = code_block.String    # code string has changed
                    try:
                        code_block.BackColor = -1
                        if bg_color:
                            code_block.BackColor = self.to_int(bg_color)
                        cursor = code_block.createTextCursorByRange(code_block)
                        cursor.CharLocale = self.nolocale
                        cursor.collapseToStart()
                        self.highlight_code(code, cursor, lexer, style)
                    finally:
                        undomanager.leaveUndoContext()

            # TEXT TABLE CELL RANGE
            elif selected_item.ImplementationName == "SwXTextTableCursor":
                table = self.doc.CurrentController.ViewCursor.TextTable
                rangename = selected_item.RangeName
                if ':' not in rangename:
                    # only one cell
                    logger.debug("Dealing with a single text table cell.")
                    code_block = table.getCellByName(rangename)
                    code = code_block.String
                    if code.strip():
                        hascode = True
                        lexer = self.getlexer(code)
                        undomanager.enterUndoContext(f"code highlight (lang: {lexer.name}, style: {stylename})")
                        if self.show_line_numbers(code_block):
                            code = code_block.String    # code string has changed
                        try:
                            code_block.BackColor = -1
                            if bg_color:
                                code_block.BackColor = self.to_int(bg_color)
                            cursor = code_block.createTextCursorByRange(code_block)
                            cursor.CharLocale = self.nolocale
                            cursor.collapseToStart()
                            self.highlight_code(code, cursor, lexer, style)
                        finally:
                            undomanager.leaveUndoContext()
                else:
                    # at least two cells
                    logger.debug("Dealing with multiple text table cells.")
                    cellrange = table.getCellRangeByName(rangename)
                    nrows, ncols = len(cellrange.Data), len(cellrange.Data[0])
                    for row in range(nrows):
                        for col in range(ncols):
                            code_block = cellrange.getCellByPosition(col, row)
                            code = code_block.String
                            if code.strip():
                                hascode = True
                                lexer = self.getlexer(code)
                                undomanager.enterUndoContext(f"code highlight (lang: {lexer.name}, style: {stylename})")
                                if self.show_line_numbers(code_block):
                                    code = code_block.String    # code string has changed
                                try:
                                    code_block.BackColor = -1
                                    if bg_color:
                                        code_block.BackColor = self.to_int(bg_color)
                                    cursor = code_block.createTextCursorByRange(code_block)
                                    cursor.CharLocale = self.nolocale
                                    cursor.collapseToStart()
                                    self.highlight_code(code, cursor, lexer, style)
                                finally:
                                    undomanager.leaveUndoContext()

            # CURSOR INSIDE DRAW/IMPRESS SHAPE
            elif selected_item.ImplementationName == "SvxUnoTextCursor":
                logger.debug("Dealing with text shape in edit mode.")
                # exit edit mode
                self.dispatcher.executeDispatch(self.frame, ".uno:SelectObject", "", 0, ())
                self.prepare_highlight()
                return

                # ### OLD CODE, intended to highlight sub text, but api's too buggy'
                # # first exit edit mode, otherwise formatting is not shown (bug?)
                # self.dispatcher.executeDispatch(self.frame, ".uno:SelectObject", "", 0, ())
                # cursor = selected_item
                # code = cursor.String
                # cdirection = cursor.compareRegionStarts(cursor.Start, cursor.End)
                # if cdirection != 0:  # a selection exists
                #     hascode = True
                #     lexer = self.getlexer(code)
                #     undomanager.enterUndoContext(f"code highlight (lang: {lexer.name}, style: {stylename})")
                #     try:
                #         cursor.CharBackColor = -1
                #         if bg_color:
                #             cursor.CharBackColor = self.to_int(bg_color)
                #         cursor.CharLocale = self.nolocale
                #         if cdirection == 1:
                #             cursor.collapseToStart()
                #         else:
                #             # if selection is done right to left inside text box, end cursor is before start cursor
                #             cursor.collapseToEnd()
                #         self.highlight_code(code, cursor, lexer, style)
                #     finally:
                #         undomanager.leaveUndoContext()

            # CALC CELL RANGE
            elif selected_item.ImplementationName in ("ScCellObj", "ScCellRangeObj", "ScCellRangesObj"):
                logger.debug('Dealing with Calc cells.')
                # exit edit mode if necessary
                self.dispatcher.executeDispatch(self.frame, ".uno:Deselect", "", 0, ())
                cells = selected_item.queryContentCells(CF_STRING).Cells
                if cells.hasElements():
                    hascode = True
                    for code_block in cells:
                        code = code_block.String
                        lexer = self.getlexer(code)
                        undomanager.enterUndoContext(f"code highlight (lang: {lexer.name}, style: {stylename})")
                        if self.show_line_numbers(code_block):
                            code = code_block.String    # code string has changed
                        try:
                            code_block.CellBackColor = -1
                            code_block.CharLocale = self.nolocale
                            if bg_color:
                                code_block.CellBackColor = self.to_int(bg_color)
                            cursor = code_block.createTextCursor()
                            cursor.gotoStart(False)
                            self.highlight_code(code, cursor, lexer, style)
                        finally:
                            undomanager.leaveUndoContext()

            else:
                logger.debug("Invalid selection (2).")
                self.msgbox(self.strings["errsel1"])
                return

            if not hascode:
                logger.debug("Current selection contains no text.")
                self.msgbox(self.strings["errsel2"])

        except AttributeError:
            self.msgbox(self.strings["errsel1"])
            logger.exception("")
        except Exception:
            self.msgbox(traceback.format_exc())
        finally:
            if self.doc.hasControllersLocked():
                self.doc.unlockControllers()
                logger.debug("Controllers unlocked.")

    def highlight_code(self, code, cursor, lexer, style):
        # caching consecutive tokens with same token type
        logger.debug(f"Starting code block highlighting (lexer: {lexer}, style: {style}).")
        lastval = ''
        lasttype = None
        for tok_type, tok_value in lexer.get_tokens(code):
            if tok_type == lasttype:
                lastval += tok_value
            else:
                if lastval:
                    cursor.goRight(len(lastval), True)  # selects the token's text
                    try:
                        tok_style = style.style_for_token(lasttype)
                        cursor.CharColor = self.to_int(tok_style['color'])
                        cursor.CharWeight = W_BOLD if tok_style['bold'] else W_NORMAL
                        cursor.CharPosture = SL_ITALIC if tok_style['italic'] else SL_NONE
                    except Exception:
                        pass
                    finally:
                        cursor.collapseToEnd()  # deselects the selected text
                lastval = tok_value
                lasttype = tok_type
        logger.debug("Terminating code block highlighting.")

    def show_line_numbers(self, code_block, isplaintext=False):
        show_linenb = self.options['ShowLineNumbers']
        startnb = self.options["LineNumberStart"]
        ratio = self.options["LineNumberRatio"]
        sep = self.options["LineNumberSeparator"]
        logger.debug(f"Starting code block numbering (show: {show_linenb}).")
        sep = sep.replace(r'\t', '\t')
        codecharheight = code_block.End.CharHeight
        nocharheight = round(codecharheight*ratio//50)/2   # round to 0.5

        c = code_block.Text.createTextCursor()
        code = c.Text.String
        if isplaintext:
            c, code = self.ensure_paragraphs(code_block)

        # check for existing line numbering and its width
        p = re.compile(r"^\s*[0-9]+[\W]?\s+", re.MULTILINE)
        try:
            lenno = min(len(f) for f in p.findall(code))
        except ValueError:
            lenno = None

        def show_numbering():
            nblignes = len(code_block.String.split('\n'))
            digits = int(log10(nblignes - 1 + startnb)) + 1
            for n, para in enumerate(code_block, start=startnb):
                # para.Start.CharHeight = nocharheight
                prefix = f'{n:>{digits}}{sep}'
                para.Start.setString(prefix)
                c.gotoRange(para.Start, False)
                c.goRight(len(prefix), True)
                c.CharHeight = nocharheight

        def hide_numbering():
            for para in code_block:
                if p.match(para.String):
                    para.CharHeight = codecharheight
                    para.String = para.String[lenno:]

        res = False
        if show_linenb:
            if not lenno:
                show_numbering()
                res = True
            else:
                # numbering already exists, but let's replace it anyway,
                # as its format may differ from current settings.
                hide_numbering()
                show_numbering()
                res = True
        elif lenno:
            hide_numbering()
            res = True
        logger.debug("Terminating code block numbering.")
        return res

    def ensure_paragraphs(self, selected_code):
        '''Ensure the selection does not contains part of paragraphs.'''

        # Cursor could start or end in the middle of a code line, when plain text selected.
        # So let's expand it to the entire paragraphs.
        c = selected_code.Text.createTextCursorByRange(selected_code)
        c.gotoStartOfParagraph(False)
        c.gotoRange(selected_code.End, True)
        c.gotoEndOfParagraph(True)
        return c, c.String


# Component registration
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(CodeHighlighter, "ooo.ext.code-highlighter.impl", (),)


# exposed functions for development stages only
# uncomment corresponding entry in ../META_INF/manifest.xml to add them as framework scripts
def highlight(event=None):
    ctx = XSCRIPTCONTEXT.getComponentContext()
    highlighter = CodeHighlighter(ctx)
    highlighter.do_highlight()


def highlight_previous(event=None):
    ctx = XSCRIPTCONTEXT.getComponentContext()
    highlighter = CodeHighlighter(ctx)
    highlighter.do_highlight_previous()
