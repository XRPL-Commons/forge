"""Validation for the closed JSON Schema subset emitted by Forge harnesses."""
import re


def schema_check(value, spec):
    """Validate the closed subset emitted by the installed harness schema.

    This remains independent of a provider's claim that structured output was
    valid. Pipeline roles additionally pass the production result validator.
    """
    if 'anyOf' in spec:
        for choice in spec['anyOf']:
            try:
                schema_check(value, choice)
                break
            except ValueError:
                pass
        else:
            raise ValueError('result_schema_invalid')
    types = dict(object=dict, array=list, string=str, integer=int, number=(int,float), boolean=bool, null=type(None))
    kind = spec.get('type')
    if kind:
        choices = kind if isinstance(kind,list) else [kind]
        if not any(type(value) in (types[k] if isinstance(types[k],tuple) else (types[k],)) for k in choices):
            raise ValueError('result_schema_invalid')
    if 'enum' in spec and value not in spec['enum']:
        raise ValueError('result_schema_invalid')
    if isinstance(value, dict):
        if any(k not in value for k in spec.get('required',())) or spec.get('additionalProperties') is False and set(value)-set(spec.get('properties',{})):
            raise ValueError('result_schema_invalid')
        for key, child in value.items():
            if key in spec.get('properties',{}):schema_check(child,spec['properties'][key])
    if isinstance(value,list):
        if not spec.get('minItems',0)<=len(value)<=spec.get('maxItems',10000):raise ValueError('result_schema_invalid')
        for child in value:schema_check(child,spec.get('items',{}))
    if isinstance(value,str):
        if not spec.get('minLength',0)<=len(value)<=spec.get('maxLength',4*1048576):raise ValueError('result_schema_invalid')
        if 'pattern' in spec and re.search(spec['pattern'],value) is None:raise ValueError('result_schema_invalid')
    if type(value) in (int,float) and not spec.get('minimum',float('-inf'))<=value<=spec.get('maximum',float('inf')):
        raise ValueError('result_schema_invalid')

