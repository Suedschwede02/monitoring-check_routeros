#!/usr/bin/env python3
# SPDX-FileCopyrightText: PhiBo DinoTools (2021)
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import re
import ssl
from typing import Any, Dict, List, Optional, Tuple

import click
import librouteros
import librouteros.query
import nagiosplugin

logger = logging.getLogger('nagiosplugin')


class BooleanContext(nagiosplugin.Context):
    def performance(self, metric, resource):
        return nagiosplugin.performance.Performance(
            label=metric.name,
            value=1 if metric.value else 0
        )


class RouterOSCheckResource(nagiosplugin.Resource):
    def __init__(self, cmd_options: Dict[str, Any]):
        self._cmd_options = cmd_options

    def _connect_api(self) -> librouteros.api.Api:
        def wrap_socket(socket):
            server_hostname: Optional[str] = self._cmd_options["hostname"]
            if server_hostname is None:
                server_hostname = self._cmd_options["host"]
            return ssl_ctx.wrap_socket(socket, server_hostname=server_hostname)

        logger.info("Connecting to device ...")
        port = self._cmd_options["port"]
        extra_kwargs = {}
        if self._cmd_options["ssl"]:
            if port is None:
                port = 8729

            context_kwargs = {}
            if self._cmd_options["ssl_cafile"]:
                context_kwargs["cafile"] = self._cmd_options["ssl_cafile"]
            if self._cmd_options["ssl_capath"]:
                context_kwargs["capath"] = self._cmd_options["ssl_capath"]

            ssl_ctx = ssl.create_default_context(**context_kwargs)

            if self._cmd_options["ssl_force_no_certificate"]:
                ssl_ctx.check_hostname = False
                ssl_ctx.set_ciphers("ADH:@SECLEVEL=0")
            elif not self._cmd_options["ssl_verify"]:
                # We have do disable hostname check if we disable certificate verification
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            elif not self._cmd_options["ssl_verify_hostname"]:
                ssl_ctx.check_hostname = False

            extra_kwargs["ssl_wrapper"] = wrap_socket
        else:
            if port is None:
                port = 8728

        api = librouteros.connect(
            host=self._cmd_options["host"],
            username=self._cmd_options["username"],
            password=self._cmd_options["password"],
            port=port,
            **extra_kwargs
        )
        return api


class ScalarPercentContext(nagiosplugin.ScalarContext):
    def __init__(self, name, total_name: str, warning=None, critical=None,
                 fmt_metric='{name} is {valueunit}', result_cls=nagiosplugin.Result):
        super(ScalarPercentContext, self).__init__(name, fmt_metric=fmt_metric, result_cls=result_cls)

        self._warning = warning
        self._critical = critical
        self._total_name = total_name
        self.warning = None
        self.critical = None

    def _prepare_ranges(self, metric, resource):
        def replace(m):
            if m.group("unit") == "%":
                return str(float(total_value) * (float(m.group("value")) / 100))
            else:
                raise ValueError("Unable to convert type")

        if self.warning is not None and self.critical is not None:
            return

        total_value = getattr(resource, self._total_name)
        regex = re.compile(r"(?P<value>\d+)(?P<unit>[%])")

        print(regex.sub(replace, self._warning))
        print(regex.sub(replace, self._critical))

        self.warning = nagiosplugin.Range(regex.sub(replace, self._warning))
        self.critical = nagiosplugin.Range(regex.sub(replace, self._critical))

    def evaluate(self, metric, resource):
        self._prepare_ranges(metric, resource)
        return super(ScalarPercentContext, self).evaluate(metric, resource)

    def performance(self, metric, resource):
        self._prepare_ranges(metric, resource)
        return super(ScalarPercentContext, self).performance(metric, resource)


@click.group()
@click.option("--host", required=True)
@click.option("--hostname")
@click.option("--port", default=None)
@click.option("--username", required=True)
@click.option("--password", required=True)
@click.option("--ssl/--no-ssl", "use_ssl", default=True)
@click.option("--ssl-cafile")
@click.option("--ssl-capath")
@click.option("--ssl-force-no-certificate", is_flag=True, default=False)
@click.option("--ssl-verify/--no-ssl-verify", default=True)
@click.option("--ssl-verify-hostname/--no-ssl-verify-hostname", default=True)
@click.option("-v", "--verbose", count=True)
@click.pass_context
def cli(ctx, host: str, hostname: Optional[str], port: int, username: str, password: str,
        use_ssl: bool, ssl_cafile: Optional[str], ssl_capath: Optional[str], ssl_force_no_certificate: bool,
        ssl_verify: bool, ssl_verify_hostname: bool, verbose: int):
    ctx.ensure_object(dict)
    ctx.obj["host"] = host
    ctx.obj["hostname"] = hostname
    ctx.obj["port"] = port
    ctx.obj["username"] = username
    ctx.obj["password"] = password
    ctx.obj["ssl"] = use_ssl
    ctx.obj["ssl_cafile"] = ssl_cafile
    ctx.obj["ssl_capath"] = ssl_capath
    ctx.obj["ssl_force_no_certificate"] = ssl_force_no_certificate
    ctx.obj["ssl_verify"] = ssl_verify
    ctx.obj["ssl_verify_hostname"] = ssl_verify_hostname
    ctx.obj["verbose"] = verbose


#########################
# Check: Interface VRRP #
#########################
class InterfaceVrrpCheck(RouterOSCheckResource):
    name = "VRRP"

    def __init__(self, cmd_options, name, master_must):
        super().__init__(cmd_options=cmd_options)

        self._name = name
        self.backup = None
        self.disabled = None
        self.enabled = None
        self.invalid = None
        self.master = None
        self.master_must = master_must
        self.running = None

    def probe(self):
        key_name = librouteros.query.Key("name")
        api = self._connect_api()

        logger.info("Fetching data ...")
        call = api.path(
            "/interface/vrrp"
        ).select(
            key_name,
            librouteros.query.Key("backup"),
            librouteros.query.Key("disabled"),
            librouteros.query.Key("invalid"),
            librouteros.query.Key("master"),
            librouteros.query.Key("running"),
        ).where(
            key_name == self._name
        )
        results = tuple(call)
        result = results[0]

        self.disabled = result["disabled"]
        self.enabled = not self.disabled

        yield nagiosplugin.Metric(
            name="disabled",
            value=self.disabled,
        )

        if self.enabled:
            for n in ("backup", "invalid", "master", "running"):
                if n not in result:
                    continue

                setattr(self, n, result[n])
                yield nagiosplugin.Metric(
                    name=n,
                    value=result[n],
                )


class InterfaceVrrpDisabled(BooleanContext):
    def evaluate(self, metric, resource: InterfaceVrrpCheck):
        if metric.value is True:
            return self.result_cls(nagiosplugin.state.Warn, "VRRP is disabled", metric)
        return self.result_cls(nagiosplugin.state.Ok)


class InterfaceVrrpInvalid(BooleanContext):
    def evaluate(self, metric, resource: InterfaceVrrpCheck):
        if metric.value is True:
            return self.result_cls(
                state=nagiosplugin.state.Warn,
                hint="VRRP config is invalid"
            )
        return self.result_cls(nagiosplugin.state.Ok)


class InterfaceVrrpMaster(BooleanContext):
    def evaluate(self, metric, resource: InterfaceVrrpCheck):
        if not metric.value and resource.master_must:
            return self.result_cls(
                state=nagiosplugin.state.Warn,
                hint="VRRP interface is not master"
            )
        return self.result_cls(nagiosplugin.state.Ok)


@cli.command("interface.vrrp")
@click.option("--name", required=True)
@click.option("--master", default=False)
@click.pass_context
def interface_vrrp(ctx, name, master):
    check = nagiosplugin.Check(
        InterfaceVrrpCheck(
            cmd_options=ctx.obj,
            name=name,
            master_must=master,
        ),
        BooleanContext("backup"),
        InterfaceVrrpDisabled("disabled"),
        InterfaceVrrpInvalid("invalid"),
        InterfaceVrrpMaster("master"),
        BooleanContext("running")
    )

    check.main(verbose=ctx.obj["verbose"])


#########################
# System Memory         #
#########################
class SystemMemoryResource(RouterOSCheckResource):
    name = "MEMORY"

    def __init__(self, cmd_options):
        super().__init__(cmd_options=cmd_options)

        self.memory_total = None

    def probe(self):
        api = self._connect_api()

        logger.info("Fetching data ...")
        call = api.path(
            "/system/resource"
        ).select(
            librouteros.query.Key("free-memory"),
            librouteros.query.Key("total-memory")
        )
        results = tuple(call)
        result = results[0]

        memory_free = result["free-memory"]
        self.memory_total = result["total-memory"]

        yield nagiosplugin.Metric(
            name="free",
            value=memory_free,
            uom="B",
            min=0,
            max=self.memory_total,
        )

        yield nagiosplugin.Metric(
            name="used",
            value=self.memory_total - memory_free,
            uom="B",
            min=0,
            max=self.memory_total,
        )


class SystemMemorySummary(nagiosplugin.summary.Summary):
    def __init__(self, result_names: List[str]):
        super().__init__()
        self._result_names = result_names

    def ok(self, results):
        msgs = []
        for result_name in self._result_names:
            msgs.append(str(results[result_name]))
        return " ".join(msgs)


@cli.command("system.memory")
@click.option("--used/--free", is_flag=True, default=True)
@click.option("--warning", required=True)
@click.option("--critical", required=True)
@click.pass_context
@nagiosplugin.guarded
def system_memory(ctx, used, warning, critical):
    check = nagiosplugin.Check(
        SystemMemoryResource(
            cmd_options=ctx.obj,
        )
    )

    if used:
        check.add(nagiosplugin.ScalarContext(
            name="free",
        ))
        check.add(ScalarPercentContext(
            name="used",
            total_name="memory_total",
            warning=warning,
            critical=critical
        ))
    else:
        check.add(ScalarPercentContext(
            name="free",
            total_name="memory_total",
            warning=f"{warning}:",
            critical=f"{critical}:"
        ))
        check.add(nagiosplugin.ScalarContext(
            name="used",
        ))

    check.add(SystemMemorySummary(
        result_names=["used"] if used else ["free"]
    ))

    check.main(verbose=ctx.obj["verbose"])


#########################
# System Uptime         #
#########################
class SystemUptimeResource(RouterOSCheckResource):
    name = "UPTIME"

    def __init__(self, cmd_options):
        super().__init__(cmd_options=cmd_options)

    def probe(self):
        api = self._connect_api()

        logger.info("Fetching data ...")
        call = api.path(
            "/system/resource"
        ).select(
            librouteros.query.Key("uptime"),
        )
        results = tuple(call)
        result = results[0]

        m = re.compile(r"(?P<hours>\d+)h(?P<minutes>\d+)m(?P<seconds>\d+)s").match(result["uptime"])
        if not m:
            raise ValueError("Unable to parse uptime")

        uptime = int(m.group("hours")) * 60 * 60 + \
            int(m.group("minutes")) * 60 + \
            int(m.group("seconds"))

        yield nagiosplugin.Metric(
            name="uptime",
            value=uptime,
            uom="s",
            min=0,
        )


@cli.command("system.uptime")
@click.pass_context
@nagiosplugin.guarded
def system_uptime(ctx):
    check = nagiosplugin.Check(
        SystemUptimeResource(
            cmd_options=ctx.obj,
        ),
        nagiosplugin.ScalarContext(
            name="uptime",
        )
    )

    check.main(verbose=ctx.obj["verbose"])


#########################
# Tool Ping Check       #
#########################
class ToolPingCheck(RouterOSCheckResource):
    name = "PING"

    def __init__(self, cmd_options, address):
        super().__init__(cmd_options=cmd_options)

        self._address = address
        self._max_packages = 1

    def probe(self):
        def strip_time(value) -> Tuple[Optional[int], Optional[str]]:
            m = re.compile(r"^(?P<time>[0-9]+)(?P<uom>.*)$").match(value)
            if m:
                return int(m.group("time")), m.group("uom")
            return None, None

        params = {"address": self._address, "count": self._max_packages}
        api = self._connect_api()

        logger.info("Call /ping command ...")
        call = api("/ping", **params)
        results = tuple(call)
        result = results[-1]

        yield nagiosplugin.Metric(
            name="packet_loss",
            value=result["packet-loss"],
            uom="%",
            min=0,
            max=100,
        )
        yield nagiosplugin.Metric(
            name="sent",
            value=result["sent"],
            min=0,
            max=self._max_packages,
        )
        yield nagiosplugin.Metric(
            name="received",
            value=result["received"],
            min=0,
            max=self._max_packages,
        )

        if result["received"] > 0:
            yield nagiosplugin.Metric(
                name="rtt_min",
                value=strip_time(result["min-rtt"])[0],
                min=0,
            )
            yield nagiosplugin.Metric(
                name="rtt_max",
                value=strip_time(result["max-rtt"])[0],
                min=0,
            )
            yield nagiosplugin.Metric(
                name="rtt_avg",
                value=strip_time(result["avg-rtt"])[0],
                min=0,
            )
            yield nagiosplugin.Metric(
                name="size",
                value=result["size"]
            )
            yield nagiosplugin.Metric(
                name="ttl",
                value=result["ttl"],
                min=0,
                max=255,
            )


@cli.command("tool.ping")
@click.option("--address", required=True)
@click.option("--packet-loss-warning")
@click.option("--packet-loss-critical")
@click.option("--ttl-warning")
@click.option("--ttl-critical")
@click.pass_context
def tool_ping(ctx, address, packet_loss_warning, packet_loss_critical, ttl_warning, ttl_critical):
    check = nagiosplugin.Check(
        ToolPingCheck(
            cmd_options=ctx.obj,
            address=address
        )
    )

    check.add(nagiosplugin.ScalarContext(
        name="packet_loss",
        warning=packet_loss_warning,
        critical=packet_loss_critical
    ))
    check.add(nagiosplugin.ScalarContext(
        name="sent"
    ))
    check.add(nagiosplugin.ScalarContext(
        name="received"
    ))

    check.add(nagiosplugin.ScalarContext(
        name="rtt_avg"
    ))
    check.add(nagiosplugin.ScalarContext(
        name="rtt_min"
    ))
    check.add(nagiosplugin.ScalarContext(
        name="rtt_max"
    ))

    check.add(nagiosplugin.ScalarContext(
        name="size"
    ))
    check.add(nagiosplugin.ScalarContext(
        name="ttl",
        warning=ttl_warning,
        critical=ttl_critical
    ))

    check.main(verbose=ctx.obj["verbose"])


if __name__ == "__main__":
    cli()
