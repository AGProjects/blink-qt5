import os
import shutil

from PyQt5.QtCore import QObject, QThread, QTimer, QCoreApplication
from PyQt5.QtWidgets import QApplication
from application.python.decorator import decorator, preserve_signature
from application.python.descriptor import classproperty
from application.python.types import Singleton
from application.system import openfile
from filecmp import cmp
from functools import partial
from gnutls.crypto import X509Certificate
from gnutls.errors import GNUTLSError
from itertools import count
from threading import Event
from sys import exc_info

from blink.event import CallFunctionEvent


__all__ = ['QSingleton', 'UniqueFilenameGenerator', 'call_in_gui_thread', 'call_later', 'copy_transfer_file', 'run_in_gui_thread', 'translate']

translate = QCoreApplication.translate


class QSingleton(Singleton, type(QObject)):
    """A metaclass for making Qt objects singletons"""


class UniqueFilenameGenerator(object):
    @classmethod
    def generate(cls, name):
        yield name
        prefix, extension = os.path.splitext(name)
        for x in count(1):
            yield "%s-%d%s" % (prefix, x, extension)


def copy_transfer_file(link, directory):
    if not link.isLocalFile():
        return link

    filename = link.fileName()
    destination = os.path.join(directory, filename)
    if destination == link.toLocalFile():
        if cmp(link.toLocalFile(), destination, True):
            return link
        raise FileNotFoundError

    is_same_file = False
    for name in UniqueFilenameGenerator.generate(destination):
        try:
            openfile(name, 'rb')
        except FileNotFoundError:
            destination = name
            break
        else:
            if cmp(link.toLocalFile(), destination, True):
                is_same_file = True
                break
            continue

    if not is_same_file:
        shutil.copy(link.toLocalFile(), directory)
    link.setPath(destination)
    return link

def call_later(interval, function, *args, **kw):
    QTimer.singleShot(int(interval*1000), lambda: function(*args, **kw))


def call_in_gui_thread(function, *args, **kw):
    application = Application.instance
    if QThread.currentThread() is Application.gui_thread:
        return function(*args, **kw)
    else:
        application.postEvent(application, CallFunctionEvent(function, args, kw))


@decorator
def run_in_gui_thread(function=None, wait=False):
    if function is not None:
        @preserve_signature(function)
        def function_wrapper(*args, **kw):
            application = Application.instance
            if QThread.currentThread() is Application.gui_thread:
                return function(*args, **kw)
            else:
                if wait:
                    executor = FunctionExecutor(function)
                    application.postEvent(application, CallFunctionEvent(executor, args, kw))
                    return executor.wait()
                else:
                    application.postEvent(application, CallFunctionEvent(function, args, kw))
        return function_wrapper
    else:
        return partial(run_in_gui_thread, wait=wait)


class Application(object):
    __attributes__ = {}

    @classproperty
    def instance(cls):
        try:
            return cls.__attributes__['instance']
        except KeyError:
            return cls.__attributes__.setdefault('instance', QApplication.instance())

    @classproperty
    def gui_thread(cls):
        try:
            return cls.__attributes__['gui_thread']
        except KeyError:
            return cls.__attributes__.setdefault('gui_thread', cls.instance.thread())


class FunctionExecutor(object):
    __slots__ = 'function', 'event', 'result', 'exception', 'traceback'

    def __init__(self, function):
        self.function = function
        self.event = Event()
        self.result = None
        self.exception = None
        self.traceback = None

    def __call__(self, *args, **kw):
        try:
            self.result = self.function(*args, **kw)
        except BaseException as exception:
            self.exception = exception
            self.traceback = exc_info()[2]
        finally:
            self.event.set()

    def wait(self):
        self.event.wait()
        if self.exception is not None:
            raise type(self.exception)(self.exception).with_traceback(self.traceback)
        else:
            return self.result

def trusted_cas(content):
    trusted_cas = []
    crt = ''
    start = False
    end = False

    content = content or ''
    content = content.decode() if isinstance(content, bytes) else content

    for line in content.split("\n"):
        if "BEGIN CERT" in line:
            start = True
            crt = line + "\n"
        elif "END CERT" in line:
            crt = crt + line + "\n"
            end = True
            start = False

            try:
                trusted_cas.append(X509Certificate(crt))
            except (GNUTLSError, ValueError) as e:
                continue
        elif start:
            crt = crt + line + "\n"

    return trusted_cas


