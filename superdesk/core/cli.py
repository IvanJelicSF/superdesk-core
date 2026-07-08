from typing import Any, Callable, Optional, Tuple, cast

from inspect import isawaitable
from functools import update_wrapper
from asyncio import run

import click
from click import Context, Group, pass_context, echo
from quart.cli import ScriptInfo

from ..flask import Blueprint, AppGroup


def with_appcontext_async(fn: Callable) -> Callable:
    """Wraps a click command with app_context and an event loop

    Allows to use an async function as a click command that is automatically
    run inside an event loop with an app context
    """

    @pass_context
    def decorator(__ctx: Context, *args: Any, **kwargs: Any) -> Any:
        async def _inner() -> Any:
            async with __ctx.ensure_object(ScriptInfo).load_app().app_context():
                try:
                    response = __ctx.invoke(fn, *args, **kwargs)
                    return await response if isawaitable(response) else response
                except RuntimeError as error:
                    if error.args[0] == "Cannot run the event loop while another loop is running":
                        echo(
                            "The appcontext cannot be used with a command that runs an event loop. "
                            "See quart#361 for more details"
                        )
                    raise

        return run(_inner())

    return update_wrapper(decorator, fn)


def with_tenant_options(fn: Callable) -> Callable:
    """Adds ``--tenant``/``--all-tenants`` options and runs the command per tenant.

    In single-tenant mode (no options given) the command runs once with the
    default tenant, exactly as before. In multi-tenant mode the options are
    required (fail closed). Must run inside an app context (the tenant
    registry lives on the app).
    """

    async def wrapper(*args: Any, tenant_ids: tuple = (), all_tenants: bool = False, **kwargs: Any) -> Any:
        from superdesk.core import get_current_async_app
        from superdesk.core.tenants import tenant_context, is_multi_tenant_enabled

        async def _invoke() -> Any:
            response = fn(*args, **kwargs)
            return await response if isawaitable(response) else response

        if all_tenants:
            tenants: list = get_current_async_app().tenants.get_all_active_sync()
        elif tenant_ids:
            tenants = list(tenant_ids)
        elif is_multi_tenant_enabled():
            raise click.UsageError("Multi-tenant mode requires --tenant <id> (repeatable) or --all-tenants")
        else:
            return await _invoke()

        results = []
        for tenant in tenants:
            with tenant_context(tenant) as bound:
                echo(f"Running for tenant '{bound.id}'")
                results.append(await _invoke())
        return results

    # keep the command's own click params, then append the tenant options
    wrapper = update_wrapper(wrapper, fn)
    wrapper = click.option(
        "--tenant", "tenant_ids", multiple=True, help="Run the command for the given tenant id (repeatable)."
    )(wrapper)
    wrapper = click.option(
        "--all-tenants", "all_tenants", is_flag=True, default=False, help="Run the command once per active tenant."
    )(wrapper)
    return wrapper


class AsyncAppGroup(AppGroup):
    """
    An extension of Quart's AppGroup to support registration of asynchronous command handlers.

    This class provides a mechanism to register asynchronous functions as command line commands,
    that are automatically wrapped with the app context (unless ``with_appcontext=False`` is provided).
    """

    def command(
        self,
        name: str | None = None,
        with_appcontext: bool = True,
        tenant_command: Optional[bool] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Callable:
        """This works exactly like the method of the same name on a regular
        :class:`click.Group` but it wraps callbacks in :func:`with_appcontext`
        if it's enabled by passing ``with_appcontext=True``.

        Unless ``tenant_command=False`` is given, the command also gets
        ``--tenant``/``--all-tenants`` options and runs once per selected
        tenant (see :func:`with_tenant_options`).
        """

        if tenant_command is None:
            tenant_command = with_appcontext

        def decorator(f: Callable) -> Callable:
            if tenant_command:
                f = with_tenant_options(f)
            if with_appcontext:
                f = with_appcontext_async(f)
            return Group.command(self, name, *args, **kwargs)(f)

        return decorator


class CommandsBlueprint(Blueprint):
    """
    Custom Blueprint that integrates AsyncAppGroup to register CLI commands that are asynchronous,
    allowing them to be used in a synchronous context by Quart's command line interface.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.cli = AsyncAppGroup()


def create_commands_blueprint(blueprint_name: str) -> Tuple[CommandsBlueprint, AsyncAppGroup]:
    """
    Create a Blueprint to organize all superdesk commands.

    By setting cli_group=None, any new CLI commands added to this blueprint
    will still be compatible with the existing `python manage.py <command-name>`
    format.

    Returns:
        Tuple[Blueprint, AppGroup]: A tuple containing the configured Blueprint
        object and its associated CLI AppGroup for command registration.
    """
    blueprint = CommandsBlueprint(blueprint_name, __name__, cli_group=None)

    return blueprint, cast(AsyncAppGroup, blueprint.cli)
