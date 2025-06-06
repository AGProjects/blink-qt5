
import json
import os
import re
import sys
try:
    from PyQt5 import sip
except ImportError:
    import sip
import urllib.request, urllib.parse, urllib.error

from PyQt5 import uic
from PyQt5.QtCore import Qt, QAbstractListModel, QModelIndex, QSortFilterProxyModel, QUrl, QUrlQuery
from PyQt5.QtGui import QIcon
from PyQt5.QtNetwork import QNetworkAccessManager
from PyQt5.QtWebKit import QWebSettings as QWebEngineSettings
from PyQt5.QtWebKitWidgets import QWebView as QWebEngineView, QWebPage as QWebEnginePage
from PyQt5.QtWidgets import QApplication, QButtonGroup, QComboBox, QMenu

from application.notification import IObserver, NotificationCenter
from application.python import Null, limit
from application.system import makedirs
from collections import defaultdict
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from zope.interface import implementer

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from sipsimple.util import user_info

from blink.configuration.settings import BlinkSettings
from blink.contacts import URIUtils
from blink.resources import ApplicationData, IconManager, Resources
from blink.sessions import SessionManager, StreamDescription
from blink.widgets.labels import Status
from blink.util import QSingleton, call_in_gui_thread, run_in_gui_thread, translate


__all__ = ['AccountModel', 'ActiveAccountModel', 'AccountSelector', 'AddAccountDialog', 'ServerToolsAccountModel', 'ServerToolsWindow']


class IconDescriptor(object):
    def __init__(self, filename):
        self.filename = filename
        self.icon = None

    def __get__(self, instance, owner):
        if self.icon is None:
            self.icon = QIcon(self.filename)
            self.icon.filename = self.filename
        return self.icon

    def __set__(self, obj, value):
        raise AttributeError("attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class AccountInfo(object):
    active_icon = IconDescriptor(Resources.get('icons/circle-dot.svg'))
    inactive_icon = IconDescriptor(Resources.get('icons/circle-grey.svg'))
    activity_icon = IconDescriptor(Resources.get('icons/circle-progress.svg'))

    def __init__(self, account):
        self.account = account
        self.registration_state = None
        self.registrar = None

    @property
    def name(self):
        return 'Bonjour' if self.account is BonjourAccount() else str(self.account.id)

    @property
    def icon(self):
        if self.registration_state == 'started':
            return self.activity_icon
        elif self.registration_state == 'succeeded':
            return self.active_icon
        else:
            return self.inactive_icon

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        elif isinstance(other, (Account, BonjourAccount)):
            return self.account == other
        elif isinstance(other, AccountInfo):
            return self.account == other.account
        return False

    def __ne__(self, other):
        return not self.__eq__(other)


@implementer(IObserver)
class AccountModel(QAbstractListModel):

    def __init__(self, parent=None):
        super(AccountModel, self).__init__(parent)
        self.accounts = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPAccountWillRegister')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidFail')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidEnd')
        notification_center.add_observer(self, name='SIPAccountDidDeactivate')
        notification_center.add_observer(self, name='BonjourAccountWillRegister')
        notification_center.add_observer(self, name='BonjourAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='BonjourAccountRegistrationDidFail')
        notification_center.add_observer(self, name='BonjourAccountRegistrationDidEnd')
        notification_center.add_observer(self, sender=AccountManager())

    def rowCount(self, parent=QModelIndex()):
        return len(self.accounts)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        account_info = self.accounts[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return account_info.name
        elif role == Qt.ItemDataRole.DecorationRole:
            return account_info.icon
        elif role == Qt.ItemDataRole.UserRole:
            return account_info
        return None

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        self.beginInsertRows(QModelIndex(), len(self.accounts), len(self.accounts))
        self.accounts.append(AccountInfo(account))
        self.endInsertRows()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, (Account, BonjourAccount)):
            position = self.accounts.index(notification.sender)
            self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        position = self.accounts.index(notification.data.account)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.accounts[position]
        self.endRemoveRows()

    def _NH_SIPAccountWillRegister(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'started'
        self.accounts[position].registrar = None
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'succeeded'
        if notification.sender is not BonjourAccount():
            registrar = notification.data.registrar
            self.accounts[position].registrar = "%s:%s:%s" % (registrar.transport, registrar.address, registrar.port)

        self.dataChanged.emit(self.index(position), self.index(position))
        notification.center.post_notification('SIPRegistrationInfoDidChange', sender=notification.sender)

    def _NH_SIPAccountDidDeactivate(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return

        self.accounts[position].registration_state = None
        self.accounts[position].registrar = None
        self.dataChanged.emit(self.index(position), self.index(position))
        notification.center.post_notification('SIPRegistrationInfoDidChange', sender=notification.sender)

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return

        reason = 'Unknown reason'

        if hasattr(notification.data, 'error'):
            reason = notification.data.error
        elif hasattr(notification.data, 'reason'):
            reason = notification.data.reason

        self.accounts[position].registration_state = 'failed (%s)' % (reason.decode() if isinstance(reason, bytes) else reason)
        self.accounts[position].registrar = None
        self.dataChanged.emit(self.index(position), self.index(position))
        notification.center.post_notification('SIPRegistrationInfoDidChange', sender=notification.sender)

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'ended'
        self.accounts[position].registrar = None
        self.dataChanged.emit(self.index(position), self.index(position))

    _NH_BonjourAccountWillRegister = _NH_SIPAccountWillRegister
    _NH_BonjourAccountRegistrationDidSucceed = _NH_SIPAccountRegistrationDidSucceed
    _NH_BonjourAccountRegistrationDidFail = _NH_SIPAccountRegistrationDidFail
    _NH_BonjourAccountRegistrationDidEnd = _NH_SIPAccountRegistrationDidEnd


class ActiveAccountModel(QSortFilterProxyModel):
    def __init__(self, model, parent=None):
        super(ActiveAccountModel, self).__init__(parent)
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        account_info = source_model.data(source_index, Qt.ItemDataRole.UserRole)
        return account_info.account.enabled


@implementer(IObserver)
class AccountSelector(QComboBox):

    def __init__(self, parent=None):
        super(AccountSelector, self).__init__(parent)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="SIPAccountManagerDidChangeDefaultAccount")
        notification_center.add_observer(self, name="SIPAccountManagerDidStart")

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidStart(self, notification):
        account = AccountManager().default_account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())




@implementer(IObserver)
class ChatAccountSelector(QComboBox):

    def __init__(self, parent=None):
        super(ChatAccountSelector, self).__init__(parent)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="SIPAccountManagerDidChangeDefaultAccount")
        notification_center.add_observer(self, name="SIPAccountManagerDidStart")
        notification_center.add_observer(self, name='BlinkSessionListSelectionChanged')
        notification_center.add_observer(self, name='BlinkSessionMessageAccountChanged')


    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidStart(self, notification):
        account = AccountManager().default_account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            current_account = self.currentData().account
            if not current_account.enabled:
                self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())
            # else:
            #     self.setCurrentIndex(model.mapFromSource(source_model.index(current_account_index)).row())

    def _NH_BlinkSessionListSelectionChanged(self, notification):
        if not notification.data.selected_session:
            return

        account = notification.data.selected_session.account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())

    def _NH_BlinkSessionMessageAccountChanged(self, notification):
        account = notification.sender.account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())


ui_class, base_class = uic.loadUiType(Resources.get('add_account.ui'))


@implementer(IObserver)
class AddAccountDialog(base_class, ui_class, metaclass=QSingleton):

    def __init__(self, parent=None):
        super(AddAccountDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.background_frame.setStyleSheet("")
        self.button_group = QButtonGroup(self)
        self.button_group.setObjectName("button_group")
        self.button_group.addButton(self.add_account_button, self.panel_view.indexOf(self.add_account_panel))
        self.button_group.addButton(self.create_account_button, self.panel_view.indexOf(self.create_account_panel))
        default_font_size = self.info_label.fontInfo().pointSizeF()
        title_font_size = limit(default_font_size + 3, max=14)
        font = self.title_label.font()
        font.setPointSizeF(title_font_size)
        self.title_label.setFont(font)
        font_metrics = self.create_status_label.fontMetrics()
        self.create_status_label.setMinimumHeight(font_metrics.height() + 2 * (font_metrics.height() + font_metrics.leading()))   # reserve space for 3 lines
        font_metrics = self.email_note_label.fontMetrics()
        self.email_note_label.setMinimumWidth(font_metrics.size(Qt.TextFlag.TextSingleLine, 'The E-mail address is used when sending voicemail').width())  # hack to make text justification look nice everywhere
        self.add_account_button.setChecked(True)
        self.panel_view.setCurrentWidget(self.add_account_panel)
        self.new_password_editor.textChanged.connect(self._SH_PasswordTextChanged)
        self.button_group.buttonClicked[int].connect(self._SH_PanelChangeRequest)
        self.accept_button.clicked.connect(self._SH_AcceptButtonClicked)
        self.display_name_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.name_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.username_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.sip_address_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.new_password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.verify_password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.email_address_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.display_name_editor.regexp = re.compile('^.*$')
        self.name_editor.regexp = re.compile('^.+$')
        self.username_editor.regexp = re.compile(r'^\w(?<=[^0_])[\w.-]{4,31}(?<=[^_.-])$', re.IGNORECASE)  # in order to enable unicode characters add re.UNICODE to flags
        self.sip_address_editor.regexp = re.compile(r'^[^@\s]+@[^@\s]+$')
        self.password_editor.regexp = re.compile('^.*$')
        self.new_password_editor.regexp = re.compile('^.{8,}$')
        self.verify_password_editor.regexp = re.compile('^$')
        self.email_address_editor.regexp = re.compile(r'^[^@\s]+@[^@\s]+$')

        account_manager = AccountManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=account_manager)

    def _get_display_name(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            return self.display_name_editor.text()
        else:
            return self.name_editor.text()

    def _set_display_name(self, value):
        self.display_name_editor.setText(value)
        self.name_editor.setText(value)

    def _get_username(self):
        return self.username_editor.text()

    def _set_username(self, value):
        self.username_editor.setText(value)

    def _get_sip_address(self):
        return self.sip_address_editor.text()

    def _set_sip_address(self, value):
        self.sip_address_editor.setText(value)

    def _get_password(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            return self.password_editor.text()
        else:
            return self.new_password_editor.text()

    def _set_password(self, value):
        self.password_editor.setText(value)
        self.new_password_editor.setText(value)

    def _get_verify_password(self):
        return self.verify_password_editor.text()

    def _set_verify_password(self, value):
        self.verify_password_editor.setText(value)

    def _get_email_address(self):
        return self.email_address_editor.text()

    def _set_email_address(self, value):
        self.email_address_editor.setText(value)

    display_name    = property(_get_display_name, _set_display_name)
    username        = property(_get_username, _set_username)
    sip_address     = property(_get_sip_address, _set_sip_address)
    password        = property(_get_password, _set_password)
    verify_password = property(_get_verify_password, _set_verify_password)
    email_address   = property(_get_email_address, _set_email_address)

    del _get_display_name, _set_display_name, _get_username, _set_username
    del _get_sip_address, _set_sip_address, _get_email_address, _set_email_address
    del _get_password, _set_password, _get_verify_password, _set_verify_password

    def _SH_AcceptButtonClicked(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            account = Account(self.sip_address)
            account.enabled = True
            account.display_name = self.display_name or None
            account.auth.password = self.password
            if account.id.domain in ('sip2sip.info', 'sylk.link'):
                account.server.settings_url = "https://blink.sipthor.net/settings.phtml"
                account.tls_name = 'sip2sip.info'
            account.save()
            account_manager = AccountManager()
            account_manager.default_account = account
            self.accept()
        else:
            self.setEnabled(False)
            self.create_status_label.value = Status(translate('add_account_dialog', 'Creating account on server...'))
            self._create_sip_account(self.username, self.password, self.email_address, self.display_name)

    def _SH_PanelChangeRequest(self, index):
        self.panel_view.setCurrentIndex(index)
        if self.panel_view.currentWidget() is self.add_account_panel:
            inputs = [self.display_name_editor, self.sip_address_editor, self.password_editor]
        else:
            inputs = [self.name_editor, self.username_editor, self.new_password_editor, self.verify_password_editor, self.email_address_editor]
        self.accept_button.setEnabled(all(input.text_valid for input in inputs))

    def _SH_PasswordTextChanged(self, text):
        self.verify_password_editor.regexp = re.compile('^%s$' % re.escape(text))

    def _SH_ValidityStatusChanged(self):
        red = '#cc0000'
        # validate the add panel
        if not self.display_name_editor.text_valid:
            self.add_status_label.value = Status(translate('add_account_dialog', "Display name cannot be empty"), color=red)
        elif not self.sip_address_editor.text_correct:
            self.add_status_label.value = Status(translate('add_account_dialog', "SIP address should be specified as user@domain"), color=red)
        elif not self.sip_address_editor.text_allowed:
            self.add_status_label.value = Status(translate('add_account_dialog', "An account with this SIP address was already added"), color=red)
        elif not self.password_editor.text_valid:
            self.add_status_label.value = Status(translate('add_account_dialog', "Password cannot be empty"), color=red)
        else:
            self.add_status_label.value = None
        # validate the create panel
        if not self.name_editor.text_valid:
            self.create_status_label.value = Status(translate('add_account_dialog', "Name cannot be empty"), color=red)
        elif not self.username_editor.text_correct:
            self.create_status_label.value = Status(translate('add_account_dialog', "Username should have 5 to 32 characters, start with a letter or non-zero digit, contain only letters, digits or .-_ and end with a letter or digit"), color=red)
        elif not self.username_editor.text_allowed:
            self.create_status_label.value = Status(translate('add_account_dialog', "The username you requested is already taken. Please choose another one and try again."), color=red)
        elif not self.new_password_editor.text_valid:
            self.create_status_label.value = Status(translate('add_account_dialog', "Password should contain at least 8 characters"), color=red)
        elif not self.verify_password_editor.text_valid:
            self.create_status_label.value = Status(translate('add_account_dialog', "Passwords do not match"), color=red)
        elif not self.email_address_editor.text_valid:
            self.create_status_label.value = Status(translate('add_account_dialog', "E-mail address should be specified as user@domain"), color=red)
        else:
            self.create_status_label.value = None
        # enable the accept button if everything is valid in the current panel
        if self.panel_view.currentWidget() is self.add_account_panel:
            inputs = [self.display_name_editor, self.sip_address_editor, self.password_editor]
        else:
            inputs = [self.name_editor, self.username_editor, self.new_password_editor, self.verify_password_editor, self.email_address_editor]
        self.accept_button.setEnabled(all(input.text_valid for input in inputs))

    def _initialize(self):
        self.display_name = user_info.fullname
        self.username = user_info.username.lower().replace(' ', '.')
        self.sip_address = ''
        self.password = ''
        self.verify_password = ''
        self.email_address = ''

    @run_in_thread('network-io')
    def _create_sip_account(self, username, password, email_address, display_name, timezone=None):
        red = '#cc0000'
        if timezone is None and sys.platform != 'win32':
            try:
                timezone = open('/etc/timezone').read().strip()
            except (OSError, IOError):
                try:
                    timezone = '/'.join(os.readlink('/etc/localtime').split('/')[-2:])
                except (OSError, IOError):
                    pass
        enrollment_data = dict(username=username.lower().encode('utf-8'),
                               password=password.encode('utf-8'),
                               email=email_address.encode('utf-8'),
                               display_name=display_name.encode('utf-8'),
                               tzinfo=timezone)
        try:
            settings = SIPSimpleSettings()
            data = urllib.parse.urlencode(dict(enrollment_data))
            response = urllib.request.urlopen(settings.server.enrollment_url, data.encode())
            response_data = json.loads(response.read().decode('utf-8').replace(r'\/', '/'))
            response_data = defaultdict(lambda: None, response_data)
            if response_data['success']:
                try:
                    passport = response_data['passport']
                    if passport is not None:
                        certificate_path = self._save_certificates(response_data['sip_address'], passport['crt'], passport['key'], passport['ca'])
                    else:
                        certificate_path = None
                except (GNUTLSError, IOError, OSError):
                    certificate_path = None
                account_manager = AccountManager()
                try:
                    account = Account(response_data['sip_address'])
                except DuplicateIDError:
                    account = account_manager.get_account(response_data['sip_address'])
                account.enabled = True
                account.display_name = display_name or None
                account.auth.password = password
                account.sip.outbound_proxy = response_data['outbound_proxy']
                account.nat_traversal.msrp_relay = response_data['msrp_relay']
                account.xcap.xcap_root = response_data['xcap_root']
                account.server.conference_server = response_data['conference_server']
                account.server.settings_url = response_data['settings_url']
                account.save()
                account_manager.default_account = account
                call_in_gui_thread(self.accept)
            elif response_data['error'] == 'user_exists':
                call_in_gui_thread(self.username_editor.addException, username)
            else:
                call_in_gui_thread(setattr, self.create_status_label, 'value', Status(response_data['error_message'], color=red))
        except (json.decoder.JSONDecodeError, KeyError):
            call_in_gui_thread(setattr, self.create_status_label, 'value', Status(translate('add_account_dialog', 'Illegal server response'), color=red))
        except urllib.error.URLError as e:
            call_in_gui_thread(setattr, self.create_status_label, 'value', Status(translate('add_account_dialog', 'Failed to contact server: %s') % e.reason, color=red))
        finally:
            call_in_gui_thread(self.setEnabled, True)

    @staticmethod
    def _save_certificates(sip_address, crt, key, ca):
        crt = crt.strip() + os.linesep
        key = key.strip() + os.linesep
        ca = ca.strip() + os.linesep
        X509Certificate(crt)
        X509PrivateKey(key)
        X509Certificate(ca)
        makedirs(ApplicationData.get('tls'))
        certificate_path = ApplicationData.get(os.path.join('tls', sip_address + '.crt'))
        certificate_file = open(certificate_path, 'w')
        os.chmod(certificate_path, 0o600)
        certificate_file.write(crt + key)
        certificate_file.close()
        ca_path = ApplicationData.get(os.path.join('tls', 'ca.crt'))
        try:
            existing_cas = open(ca_path).read().strip() + os.linesep
        except:
            certificate_file = open(ca_path, 'w')
            certificate_file.write(ca)
            certificate_file.close()
        else:
            if ca not in existing_cas:
                certificate_file = open(ca_path, 'w')
                certificate_file.write(existing_cas + ca)
                certificate_file.close()
        settings = SIPSimpleSettings()
        settings.tls.ca_list = ca_path
        settings.save()
        return certificate_path

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        self.sip_address_editor.addException(notification.data.account.id)

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        self.sip_address_editor.removeException(notification.data.account.id)

    def open_for_add(self):
        self.add_account_button.click()
        self.add_account_button.setFocus()
        self.accept_button.setEnabled(False)
        self._initialize()
        self.show()

    def open_for_create(self):
        self.create_account_button.click()
        self.create_account_button.setFocus()
        self.accept_button.setEnabled(False)
        self._initialize()
        self.show()


del ui_class, base_class


# Account server tools
#
class WebPage(QWebEnginePage):
    def __init__(self, parent=None):
        super(WebPage, self).__init__(parent)
        disable_actions = {QWebEnginePage.OpenLink, QWebEnginePage.OpenLinkInNewWindow, QWebEnginePage.OpenLinkInThisWindow, QWebEnginePage.OpenFrameInNewWindow, QWebEnginePage.DownloadLinkToDisk,
                           QWebEnginePage.OpenImageInNewWindow, QWebEnginePage.DownloadImageToDisk, QWebEnginePage.DownloadMediaToDisk}
        for action in (self.action(action) for action in disable_actions):
            action.setVisible(False)
        self.call_link_clicked = False

    def createWindow(self, type):
        return self

    def acceptNavigationRequest(self, frame, request, navigation_type):
        if navigation_type == QWebEnginePage.NavigationTypeLinkClicked and self.linkDelegationPolicy() == QWebEnginePage.DontDelegateLinks and request.url().scheme() in ('sip', 'sips'):
            blink = QApplication.instance()
            contact, contact_uri = URIUtils.find_contact(request.url().toString())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')])
            blink.main_window.raise_()
            blink.main_window.activateWindow()
            self.call_link_clicked = True
            return False
        self.call_link_clicked = False
        return super(WebPage, self).acceptNavigationRequest(frame, request, navigation_type)


class ServerToolsAccountModel(QSortFilterProxyModel):
    def __init__(self, model, parent=None):
        super(ServerToolsAccountModel, self).__init__(parent)
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        account_info = source_model.data(source_index, Qt.UserRole)
        return bool(account_info.account is not BonjourAccount() and account_info.account.enabled and account_info.account.server.settings_url)


@implementer(IObserver)
class ServerToolsWebView(QWebEngineView):

    def __init__(self, parent=None):
        super(ServerToolsWebView, self).__init__(parent)
        self.setPage(WebPage(self))
        self.access_manager = Null
        self.authenticated = False
        self.account = None
        self.user_agent = 'blink'
        self.tab = None
        self.task = None
        self.last_error = None
        self.realm = None
        self.homepage = None
        self.urlChanged.connect(self._SH_URLChanged)
        self.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        self.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)

    @property
    def query_items(self):
        all_items = ('user_agent', 'tab', 'task', 'realm')
        return [(name, value) for name, value in self.__dict__.items() if name in all_items and value is not None]

    def _get_account(self):
        return self.__dict__['account']

    def _set_account(self, account):
        notification_center = NotificationCenter()
        old_account = self.__dict__.get('account', Null)
        if account is old_account:
            return
        self.__dict__['account'] = account
        self.authenticated = False
        if old_account:
            notification_center.remove_observer(self, sender=old_account)
        if account:
            notification_center.add_observer(self, sender=account)
            self.realm = account.id.domain
        else:
            self.realm = None
        self.access_manager.authenticationRequired.disconnect(self._SH_AuthenticationRequired)
        self.access_manager.finished.disconnect(self._SH_Finished)
        self.access_manager = QNetworkAccessManager(self)
        self.access_manager.authenticationRequired.connect(self._SH_AuthenticationRequired)
        self.access_manager.finished.connect(self._SH_Finished)
        self.page().setNetworkAccessManager(self.access_manager)

    account = property(_get_account, _set_account)
    del _get_account, _set_account

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if '__id__' in notification.data.modified or 'auth.password' in notification.data.modified:
            self.authenticated = False
            self.reload()

    def _SH_AuthenticationRequired(self, reply, auth):
        if self.account and not self.authenticated:
            auth.setUser(self.account.id.username)
            auth.setPassword(self.account.auth.password)
            self.authenticated = True
        else:
            # we were already authenticated, yet it asks for the auth again. this means our credentials are not good.
            # we do not provide credentials anymore in order to fail and not try indefinitely, but we also reset the
            # authenticated status so that we try again when the page is reloaded.
            self.authenticated = False

    def _SH_Finished(self, reply):
        if reply.error() != reply.NoError:
            self.last_error = reply.errorString()
        else:
            self.last_error = None

    def _SH_URLChanged(self, url):
        query_items = dict(QUrlQuery(url).queryItems())
        self.tab = query_items.get('tab') or self.tab
        self.task = query_items.get('task') or self.task

    def load_account_page(self, account, tab=None, task=None, reset_history=False, set_home=False):
        self.tab = tab
        self.task = task
        self.account = account
        url = QUrl(account.server.settings_url)
        url_query = QUrlQuery()
        for name, value in self.query_items:
            url_query.addQueryItem(name, value)
        url.setQuery(url_query)
        if set_home:
            self.homepage = url
        if reset_history:
            self.history().clear()
            self.page().mainFrame().evaluateJavaScript('window.location.replace("{}");'.format(url.toString()))  # this will replace the current url in the history
        else:
            self.load(url)

    def load_homepage(self):
        self.load(self.homepage or self.history().itemAt(0).url())


ui_class, base_class = uic.loadUiType(Resources.get('server_tools.ui'))


@implementer(IObserver)
class ServerToolsWindow(base_class, ui_class, metaclass=QSingleton):

    def __init__(self, model, parent=None):
        super(ServerToolsWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi()
        self.setWindowTitle('Blink Server Tools')
        self.setWindowIcon(QIcon(Resources.get('icons/blink48.png')))
        self.model = model
        self.model.rowsInserted.connect(self._SH_ModelChanged)
        self.model.rowsRemoved.connect(self._SH_ModelChanged)
        self.account_button.menu().triggered.connect(self._SH_AccountButtonMenuTriggered)
        self.back_button.clicked.connect(self._SH_BackButtonClicked)
        self.back_button.triggered.connect(self._SH_NavigationButtonTriggered)
        self.forward_button.clicked.connect(self._SH_ForwardButtonClicked)
        self.forward_button.triggered.connect(self._SH_NavigationButtonTriggered)
        self.home_button.clicked.connect(self._SH_HomeButtonClicked)
        self.web_view.loadStarted.connect(self._SH_WebViewLoadStarted)
        self.web_view.loadFinished.connect(self._SH_WebViewLoadFinished)
        self.web_view.titleChanged.connect(self._SH_WebViewTitleChanged)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')

    def setupUi(self):
        super(ServerToolsWindow, self).setupUi(self)
        self.account_button.default_avatar = QIcon(Resources.get('icons/default-avatar.png'))
        self.account_button.setIcon(IconManager().get('avatar') or self.account_button.default_avatar)
        self.account_button.setMenu(QMenu(self.account_button))
        self.back_button.setMenu(QMenu(self.back_button))
        self.back_button.setEnabled(False)
        self.forward_button.setMenu(QMenu(self.forward_button))
        self.forward_button.setEnabled(False)

    def _SH_AccountButtonMenuTriggered(self, action):
        account = action.data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab=self.web_view.tab, task=self.web_view.task, reset_history=account_changed, set_home=account_changed)

    def _SH_BackButtonClicked(self):
        self.web_view.history().back()

    def _SH_ForwardButtonClicked(self):
        self.web_view.history().forward()

    def _SH_NavigationButtonTriggered(self, action):
        self.web_view.history().goToItem(action.history_item)

    def _SH_HomeButtonClicked(self):
        self.web_view.load_homepage()

    def _SH_WebViewLoadStarted(self):
        self.spinner.show()

    def _SH_WebViewLoadFinished(self, load_ok):
        self.spinner.hide()
        if not load_ok and not self.web_view.page().call_link_clicked:
            icon_path = Resources.get('icons/invalid.png')
            error_message = self.web_view.last_error or 'Unknown error'
            html = """
            <html>
             <head>
              <style>
                .icon    { width: 64px; height: 64px; float: left; }
                .message { margin-left: 74px; line-height: 64px; vertical-align: middle; }
              </style>
             </head>
             <body>
              <img class="icon" src="file:%s" />
              <div class="message">Failed to load web page: <b>%s</b></div>
             </body>
            </html>
            """ % (icon_path, error_message)
            self.web_view.blockSignals(True)
            self.web_view.setHtml(html, baseUrl=QUrl.fromLocalFile(os.path.abspath(sys.argv[0])))
            self.web_view.blockSignals(False)
        self._update_navigation_buttons()

    def _SH_WebViewTitleChanged(self, title):
        self.window().setWindowTitle(translate('server_window', 'Blink Server Tools: {}').format(title))

    def _SH_ModelChanged(self, parent_index, start, end):
        menu = self.account_button.menu()
        menu.clear()
        for row in range(self.model.rowCount()):
            account_info = self.model.data(self.model.index(row, 0), Qt.ItemDataRole.UserRole)
            action = menu.addAction(account_info.name)
            action.setData(account_info.account)

    def open_settings_page(self, account):
        account = account or self.web_view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab='settings', reset_history=account_changed, set_home=True)
        self.show()

    def open_search_for_people_page(self, account):
        account = account or self.web_view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab='contacts', task='directory', reset_history=account_changed, set_home=True)
        self.show()

    def open_history_page(self, account):
        account = account or self.web_view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab='calls', reset_history=account_changed, set_home=True)
        self.show()

    def _update_navigation_buttons(self):
        history = self.web_view.history()
        self.back_button.setEnabled(history.canGoBack())
        self.forward_button.setEnabled(history.canGoForward())
        back_menu = self.back_button.menu()
        back_menu.clear()
        for item in reversed(history.backItems(7)):
            action = back_menu.addAction(item.title())
            action.history_item = item
        forward_menu = self.forward_button.menu()
        forward_menu.clear()
        for item in history.forwardItems(7):
            action = forward_menu.addAction(item.title())
            action.history_item = item

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange', sender=BlinkSettings())

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'presence.icon' in notification.data.modified:
            self.account_button.setIcon(IconManager().get('avatar') or self.account_button.default_avatar)


del ui_class, base_class
