from django.conf import settings


def environment_banner(request):
    """
    Makes ENVIRONMENT available in every template without passing it
    explicitly from every view. Used to show a visible DEV banner so
    a development session is never mistaken for production.
    """
    return {
        "ghashem_environment": settings.ENVIRONMENT,
        "is_dev_environment": settings.ENVIRONMENT != "production",
    }