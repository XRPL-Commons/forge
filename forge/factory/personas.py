"""Who the operator is talking to: one stable persona per coding provider.

ui/app/utils/personas.ts carries the same names (tests/test_personas.py keeps
the two in step). A worker is a fresh container every turn, but the operator
always talks to the same named agent for a provider, and sees when it changes.
"""

PERSONAS = {
    "claude": {"name": "Ember", "provider_label": "Claude"},
    "codex": {"name": "Bolt", "provider_label": "Codex"},
    "grok": {"name": "Nova", "provider_label": "Grok"},
}


def persona(provider):
    """The provider's persona; an unknown provider is named after itself."""
    if provider in PERSONAS:
        return {"provider": provider, **PERSONAS[provider]}
    label = str(provider or "agent")[:30]
    label = label[:1].upper() + label[1:]
    return {"provider": provider, "name": label, "provider_label": label}
