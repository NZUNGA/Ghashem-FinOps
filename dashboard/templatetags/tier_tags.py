from django import template
from core.access import TIER_RANK

register = template.Library()


@register.simple_tag(takes_context=True)
def tier_enabled(context, minimum_tier):
    """
    Returns True if the current client's tier meets
    the minimum requirement.
    Superusers always get True.
    """
    client = context.get("client")
    request = context.get("request")
    if not client:
        return False
    if request and request.user.is_superuser:
        return True
    return (
        TIER_RANK.get(client.tier, 0) >=
        TIER_RANK.get(minimum_tier, 99)
    )


@register.filter
def tier_label(tier_slug, tier_display_dict):
    """
    Short tier name (e.g. "Advisory") for a given tier slug, looked up
    in a {slug: full_choice_label} dict such as dict(Client.TIER_CHOICES)
    — needed because Django templates have no built-in way to index a
    dict by a variable key (`tier_display.required_tier` would look up
    the literal key "required_tier", not the value of that variable).
    Strips the " — pricing details" suffix TIER_CHOICES labels carry,
    since upgrade_required.html only wants the plan name itself.
    """
    if not tier_display_dict:
        return tier_slug
    full = tier_display_dict.get(tier_slug, tier_slug)
    return full.split(" — ")[0]
