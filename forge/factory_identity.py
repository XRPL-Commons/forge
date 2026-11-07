"""The operator's factory name, separate from machine and credential identity."""
import re

from .store.configuration import Configuration
from .store.runtime import required


def initialize(root, name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', name):
        raise ValueError('Choose a factory name of 1–64 letters, digits, spaces, dots, underscores or hyphens')
    configuration = Configuration(required(root))
    current = configuration.get('factory_identity')
    if current:
        if current['settings']['name'] != name:
            raise ValueError('This factory already has a different name; initialization cannot rename it')
        return
    configuration.replace('factory_identity', {'name': name}, expected_revision=0)


def display_name(root):
    value = Configuration(required(root)).get('factory_identity')
    return value['settings']['name'] if value else None
