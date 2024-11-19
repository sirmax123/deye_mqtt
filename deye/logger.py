import os
import sys
import logging
import traceback

# Override format method in logging.Formatter class
# to add default colors to logs.
# Added constants:
#  - default_log_colors
#  - color_codes
# Added methods:
# - colorize_levelname
# To format method added line:
#   record.levelname = self.colorize_levelname(record.levelname)
class ColoredFormatter(logging.Formatter):

    default_log_colors = {
        'DEBUG':    'bold_green',
        'INFO':     'yellow',
        'WARNING':  'bold_yellow',
        'ERROR':    'bold_red',
        'CRITICAL': 'red',
    }

    color_codes = {
        'red':         '\x1b[31m',
        'bold_red':    '\x1b[1m\x1b[31m',
        'yellow':      '\x1b[33m',
        'bold_yellow': '\x1b[1m\x1b[33m',
        'bold_green':  '\x1b[1m\x1b[32m',
        'green':       '\x1b[32m',
        'reset':       '\033[m'
    }

    def __init__(self, fmt=None, datefmt=None):
        super(ColoredFormatter, self).__init__(fmt, datefmt)

    # Simple method to add color codes to logname
    def colorize_levelname(self, logname):
        color_code = self.default_log_colors[logname]
        return "{color_code}{logname}{reset_color_code}".format(color_code=self.color_codes[color_code],
                                                                logname=logname,
                                                                reset_color_code=self.color_codes['reset'])

    def format(self, record):
        record.levelname = self.colorize_levelname(record.levelname)
        return super(ColoredFormatter, self).format(record)


class CommonLogger(logging.getLoggerClass()):
    """ Custom Logger class to provide formatting and exception handling """
    def __init__(self, name):
        super(CommonLogger, self).__init__(name)
        handler = logging.StreamHandler()
        format_string = '%(asctime)s [%(levelname)-5s]' + name + ': %(message)s'

        # тут можно что-то напечтать - это удобно что бы понимать когда какой-то класс
        # вызывает еще раз создания логгера (и сообщения начинают повторяться)
        #print(format_string)
        # Отключить цвета при выводе не в терминал
        if sys.stdout.isatty():
            formatter = ColoredFormatter(format_string)
        else:
            formatter = logging.Formatter(format_string)

        handler.setFormatter(formatter)
        self.addHandler(handler)

    def exception(self, *args):
        for l in traceback.format_exc().splitlines():
            self.error(l)
        if args:
            self.error(*args)


def getLogger(name):
    """ Set our logger class and return preconfigured logger """
    logging.setLoggerClass(CommonLogger)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if "DEBUG" in os.environ:
        logger.setLevel(logging.DEBUG)
    return logger

