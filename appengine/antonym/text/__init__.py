import random

from antonym.core import AppException


class TextException(AppException): pass

class InvalidMixtureException(TextException): pass