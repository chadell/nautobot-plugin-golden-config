"""Helper functions."""
# pylint: disable=raise-missing-from
from functools import partial
from typing import Optional, List
import jinja2
from jinja2 import exceptions as jinja_errors

from django.db.models import Q
from django.http import HttpRequest

from nautobot.dcim.models import Device
from nautobot.dcim.filters import DeviceFilterSet
from nautobot.utilities.utils import render_jinja2
from nautobot.users.models import User
from nautobot.extras.models.secrets import SecretsGroup
from nautobot.extras.choices import SecretsGroupAccessTypeChoices
from nautobot.utilities.permissions import permission_is_exempt

from nornir_nautobot.exceptions import NornirNautobotException
from netutils.utils import jinja2_convenience_function

from nautobot_golden_config import models
from nautobot_golden_config.utilities.constant import PLUGIN_CFG
from nautobot_golden_config.utilities.graphql import graph_ql_query

FIELDS = {
    "platform",
    "tenant_group",
    "tenant",
    "region",
    "site",
    "role",
    "rack",
    "rack_group",
    "manufacturer",
    "device_type",
}


def get_job_filter(data=None):
    """Helper function to return a the filterable list of OS's based on platform.slug and a specific custom value."""
    if not data:
        data = {}
    query = {}

    # Translate instances from FIELDS set to list of primary keys
    for field in FIELDS:
        if data.get(field):
            query[f"{field}_id"] = data[field].values_list("pk", flat=True)

    # Build tag query based on slug values for each instance
    if data.get("tag"):
        query.update({"tag": data["tag"].values_list("slug", flat=True)})

    # Handle case where object is from single device run all.
    if data.get("device") and isinstance(data["device"], Device):
        query.update({"id": [str(data["device"].pk)]})
    elif data.get("device"):
        query.update({"id": data["device"].values_list("pk", flat=True)})

    raw_qs = Q()
    # If scope is set to {} do not loop as all devices are in scope.
    if not models.GoldenConfigSetting.objects.filter(dynamic_group__filter__iexact="{}").exists():
        for obj in models.GoldenConfigSetting.objects.all():
            raw_qs = raw_qs | obj.dynamic_group.generate_query()

    base_qs = Device.objects.filter(raw_qs)

    if not base_qs.exists():
        raise NornirNautobotException(
            "The base queryset didn't find any devices. Please check the Golden Config Setting scope."
        )
    devices_filtered = DeviceFilterSet(data=query, queryset=base_qs)
    if not devices_filtered.qs.exists():
        raise NornirNautobotException(
            "The provided job parameters didn't match any devices detected by the Golden Config scope. Please check the scope defined within Golden Config Settings or select the correct job parameters to correctly match devices."
        )
    devices_no_platform = devices_filtered.qs.filter(platform__isnull=True)
    if devices_no_platform.exists():
        raise NornirNautobotException(
            f"The following device(s) {', '.join([device.name for device in devices_no_platform])} have no platform defined. Platform is required."
        )

    return devices_filtered.qs


def null_to_empty(val):
    """Convert to empty string if the value is currently null."""
    if not val:
        return ""
    return val


def verify_settings(logger, global_settings, attrs):
    """Helper function to verify required attributes are set before a Nornir play start."""
    for item in attrs:
        if not getattr(global_settings, item):
            logger.log_failure(None, f"Missing the required global setting: `{item}`.")
            raise NornirNautobotException()


def render_jinja_template(obj, logger, template):
    """
    Helper function to render Jinja templates.

    Args:
        obj (Device): The Device object from Nautobot.
        logger (NornirLogger): Logger to log error messages to.
        template (str): A Jinja2 template to be rendered.

    Returns:
        str: The ``template`` rendered.

    Raises:
        NornirNautobotException: When there is an error rendering the ``template``.
    """
    try:
        return render_jinja2(template_code=template, context={"obj": obj})
    except jinja_errors.UndefinedError as error:
        error_msg = (
            "Jinja encountered and UndefinedError`, check the template for missing variable definitions.\n"
            f"Template:\n{template}"
        )
        logger.log_failure(obj, error_msg)
        raise NornirNautobotException from error
    except jinja_errors.TemplateSyntaxError as error:  # Also catches subclass of TemplateAssertionError
        error_msg = (
            f"Jinja encountered a SyntaxError at line number {error.lineno},"
            f"check the template for invalid Jinja syntax.\nTemplate:\n{template}"
        )
        logger.log_failure(obj, error_msg)
        raise NornirNautobotException from error
    # Intentionally not catching TemplateNotFound errors since template is passes as a string and not a filename
    except jinja_errors.TemplateError as error:  # Catches all remaining Jinja errors
        error_msg = (
            "Jinja encountered an unexpected TemplateError; check the template for correctness\n"
            f"Template:\n{template}"
        )
        logger.log_failure(error_msg)
        raise NornirNautobotException from error


def get_device_to_settings_map(queryset):
    """Helper function to map settings to devices."""
    device_to_settings_map = {}
    for device in queryset:
        dynamic_group = device.dynamic_groups.exclude(golden_config_setting__isnull=True).order_by(
            "-golden_config_setting__weight"
        )
        if dynamic_group.exists():
            device_to_settings_map[device.id] = dynamic_group.first().golden_config_setting
    return device_to_settings_map


def get_secret_by_secret_group_slug(
    user: User,
    secrets_group_slug: str,
    secret_type: str,
    secret_access_type: Optional[str] = SecretsGroupAccessTypeChoices.TYPE_GENERIC,
) -> Optional[str]:
    """Gets the secret from a Secret Group slug. To be used as a Jinja filter.

    We assume that there is only one secret group corresponding to a model.

    Args:
        user (User): User object that performs API call to render push template with secrets.
        secrets_group_slug (str): Secrets Group slug. It needs to be part of the GraphQL query.
        secret_type (str): Type of secret, such as "username", "password", "token", "secret", or "key".
        secret_access_type (Optional[str], optional): Type of secret such as "Generic", "gNMI", "HTTP(S)". Defaults to "Generic".

    Returns:
        Optional[str] : Secret value. None if there is no match. An error string if there is an error.
    """
    permission_secrets_group = "extras.view_secretsgroup"

    # Bypass restriction for superusers and exempt views
    if user.is_superuser or permission_is_exempt(permission_secrets_group):
        pass
    # User is anonymous or has not been granted the requisite permission
    elif not user.is_authenticated or permission_secrets_group not in user.get_all_permissions():
        return f"You have no permission to read this secret {secrets_group_slug}."

    secrets_group = SecretsGroup.objects.get(slug=secrets_group_slug)
    if secrets_group:
        return secrets_group.get_secret_value(
            access_type=secret_access_type,
            secret_type=secret_type,
        )

    return None


class RenderConfigToPushError(Exception):
    """Exception related to Render Configuration to Push operations."""


def render_secrets(config_to_push: str, configs: models.GoldenConfig, request: HttpRequest) -> str:
    """Renders secrets using the get_secrets filter.

    This method is defined to render an already rendered intended configuration, but which have used the Jinja
    `{% raw %}` tag to skip the first render (because the first one gets persisted, and for secrets we don't want it).
    It also support chaining with some Netutils encrypt filters.

    .. rubric:: Example Jinja render_secrets filters usage
    .. highlight:: jinja
    .. code-block:: jinja
        ppp pap sent-username {{ secrets_group["slug"] | get_secret_by_secret_group_slug("password") | encrypt_type7 }}

    Returns:
        str : Return a string, with the rendered intended configuration with secrets, or an error message.

    """
    device = configs.device
    jinja_env = jinja2.Environment(autoescape=True)

    for name, func in jinja2_convenience_function().items():
        # Only importing the encrypt helpers as complements to get_secrets filter
        if name in ["encrypt_type5", "encrypt_type7"]:
            jinja_env.filters[name] = func

    # Wrapper for get_secret filter that includes user argument to ensure
    # that secrets are only rendered by authorized users.
    # To call this method, the view verifies that it's an authenticated request.
    jinja_env.filters["get_secret_by_secret_group_slug"] = partial(get_secret_by_secret_group_slug, request.user)

    # Defining the starting intended configuration reference
    # either a config_to_push, or the default configs.intended_config
    config_to_render = config_to_push or configs.intended_config

    if not config_to_render:
        raise RenderConfigToPushError("No reference Intended configuration to render secrets from")

    try:
        template = jinja_env.from_string(config_to_render)
    except jinja_errors.TemplateAssertionError as tae:
        return f"Jinja encountered an TemplateAssertionError: '{tae}'; check the template for correctness"

    settings = get_device_to_settings_map(Device.objects.filter(pk=device.pk))[device.id]
    _, device_data = graph_ql_query(request, device, settings.sot_agg_query.query)

    try:
        return template.render(device_data)

    except jinja_errors.UndefinedError as error:
        raise RenderConfigToPushError(
            f"Jinja encountered and UndefinedError: {error}, check the template for missing variable definitions.\n"
        ) from error
    except jinja_errors.TemplateSyntaxError as error:  # Also catches subclass of TemplateAssertionError
        raise RenderConfigToPushError(
            f"Jinja encountered a SyntaxError at line number {error.lineno},"
            f"check the template for invalid Jinja syntax.\n"
        ) from error
    except jinja_errors.TemplateError as error:  # Catches all remaining Jinja errors
        raise RenderConfigToPushError(
            "Jinja encountered an unexpected TemplateError; check the template for correctness\n"
        ) from error


# TODO: Remove it's the previous implementation
# def get_config_to_push(configs: models.GoldenConfig, request: HttpRequest) -> str:
#     """Renders final configuration push artifact from intended configuration.

#     It chains multiple callables to transform an intended configuration into a configuration that can be pushed.
#     Each callable should match the following signature:
#     `my_callable_function(config_to_push: str, configs: models.GoldenConfig, request: HttpRequest)`
#     """
#     config_to_push = ""

#     # Available functions to create the final intended configuration to push
#     config_push_callable = [render_secrets]
#     if PLUGIN_CFG.get("config_push_callable"):
#         config_push_callable = PLUGIN_CFG["config_push_callable"]

#     # Actual callable subscribed to post processing the intended configuration
#     config_push_subscribed = [render_secrets]
#     if PLUGIN_CFG.get("config_push_subscribed"):
#         config_push_subscribed = PLUGIN_CFG["config_push_subscribed"]

#     for func in config_push_callable:
#         if func in config_push_subscribed:
#             try:
#                 config_to_push = func(config_to_push, configs, request)
#             except RenderConfigToPushError as error:
#                 return f"Find an error rendering the configuration to push: {error}"

#     return config_to_push


def get_config_to_push(
    configs: models.GoldenConfig, request: HttpRequest, custom_subscribed: Optional[List[str]] = None
) -> str:
    """Renders final configuration push artifact from intended configuration.

    It chains multiple callables to transform an intended configuration into a configuration that can be pushed.
    Each callable should match the following signature:
    `my_callable_function(config_to_push: str, configs: models.GoldenConfig, request: HttpRequest, custom_subscribed: Optional[List[str]])`

    Args:
        configs (models.GoldenConfig): Golden Config object per device, to retrieve device info, and related configs.
        request (HttpRequest): HTTP request for context.
        custom_subscribed (List[str]): List with the order of the callables to be processed.
    """
    config_to_push = ""

    # Available functions to create the final intended configuration to push
    config_push_callable = [render_secrets] + PLUGIN_CFG.get("config_push_callable", [])

    # Actual callable subscribed to post processing the intended configuration
    if custom_subscribed:
        config_push_subscribed = custom_subscribed
    else:
        config_push_subscribed = ["render_secrets"]
        if PLUGIN_CFG.get("config_push_subscribed"):
            config_push_subscribed = PLUGIN_CFG["config_push_subscribed"]

    for func_name in config_push_subscribed:
        for func in config_push_callable:
            if func.__name__ == func_name:
                try:
                    config_to_push = func(config_to_push, configs, request)
                except RenderConfigToPushError as error:
                    return f"Find an error rendering the configuration to push: {error}"

    return config_to_push
