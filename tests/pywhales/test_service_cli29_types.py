"""Static service create, update, inspect and ps contracts, checked by ty."""

from datetime import timedelta
from typing import assert_type

from pywhales import DockerClient, Service
from pywhales.components.task.cli_wrapper import Task


def _check_service_create(client: DockerClient) -> None:
    assert_type(
        client.service.create(
            "img",
            ["cmd"],
            name="web",
            replicas=2,
            mode="global-job",
            publish=[(80,), (8080, 80), (8080, 80, "tcp"), {"published": 8080}],
            config=[{"source": "c", "uid": 1000}],
            user="1000:1000",
            workdir="/srv",
            tty=True,
            read_only=False,
            stop_signal="SIGTERM",
            stop_grace_period=timedelta(seconds=3),
            log_opts={"max-size": "1m"},
            sysctls={"net.core.somaxconn": "1024"},
            ulimits=["nofile=1024:2048"],
            reserve_cpu=0.5,
            reserve_memory="64m",
            restart_delay=1,
            restart_window=timedelta(),
            placement_prefs=["spread=node.labels.zone"],
            with_registry_auth=True,
            container_labels={"a": "b"},
            credential_spec="file://spec.json",
            health_start_interval=1,
            max_concurrent=1,
            memory_swap=-1,
            memory_swappiness=0,
            no_resolve_image=True,
            oom_score_adj=-500,
            replicas_max_per_node=1,
            update_parallelism=1,
            update_failure_action="rollback",
            rollback_order="stop-first",
            timeout=1.0,
        ),
        Service,
    )
    assert_type(client.service.create("img", None, mode="replicated"), Service)


def _check_service_update(client: DockerClient, service: Service) -> None:
    assert_type(
        client.service.update(
            "svc",
            args=["sleep", "10"],
            cap_add=["NET_ADMIN"],
            cap_drop=["MKNOD"],
            config_add=[{"source": "c"}],
            config_rm=["old"],
            constraint_add=["node.role==worker"],
            constraint_rm=["node.role==manager"],
            container_label_add={"a": "b"},
            container_label_rm=["a"],
            credential_spec="file://spec.json",
            dns_add=["1.1.1.1"],
            dns_rm=["8.8.8.8"],
            dns_option_add=["ndots:2"],
            dns_option_rm=["timeout:1"],
            dns_search_add=["example.com"],
            dns_search_rm=["example.org"],
            endpoint_mode="dnsrr",
            entrypoint="/entry",
            env_add={"A": "1"},
            env_rm=["B"],
            generic_resource_add=["gpu=2"],
            generic_resource_rm=["gpu"],
            group_add=["wheel"],
            group_rm=["staff"],
            health_cmd="true",
            health_interval=1,
            health_retries=3,
            health_start_interval=timedelta(seconds=1),
            health_start_period=1,
            health_timeout=1,
            host_add={"db": "10.0.0.5"},
            host_rm={"cache": "10.0.0.6"},
            hostname="box",
            init=None,
            isolation="process",
            label_add={"k": "v"},
            label_rm=["old"],
            limit_cpu=0.5,
            limit_memory="1g",
            limit_pids=100,
            log_driver="json-file",
            log_opts={"max-size": "1m"},
            max_concurrent=1,
            memory_swap="2g",
            memory_swappiness=-1,
            mount_add=[{"type": "volume", "target": "/v"}],
            mount_rm=["/old"],
            network_add=["net"],
            network_rm=["old"],
            no_healthcheck=True,
            no_resolve_image=True,
            oom_score_adj=0,
            placement_pref_add=["spread=node.labels.zone"],
            placement_pref_rm=["spread=node.labels.rack"],
            publish_add=[(8080, 80), {"published": 8081, "target": 81}],
            publish_rm=[(443,)],
            read_only=False,
            replicas_max_per_node=2,
            reserve_cpu=0.25,
            reserve_memory="32m",
            restart_condition="on-failure",
            restart_delay=timedelta(seconds=1),
            restart_max_attempts=3,
            restart_window=60,
            rollback=False,
            secret_add=[{"source": "s"}],
            secret_rm=["old"],
            stop_grace_period=10,
            stop_signal="SIGINT",
            sysctl_add={"net.core.somaxconn": "1024"},
            sysctl_rm=["kernel.shmmax"],
            tty=True,
            ulimit_add=["nofile=1:2"],
            ulimit_rm=["nproc"],
            user="1000",
            workdir="/srv",
            update_order="start-first",
            timeout=1.0,
        ),
        None,
    )
    assert_type(
        service.update(
            True,
            True,
            "img",
            True,
            True,
            2,
            args=[],
            env_add={"A": "1"},
            init=False,
            tty=None,
            read_only=True,
            publish_rm=[{"target": 80}],
            rollback=True,
            timeout=1.0,
        ),
        None,
    )


def _check_service_inspect(client: DockerClient, pretty: bool) -> None:
    assert_type(client.service.inspect("web"), Service)
    assert_type(client.service.inspect(["web"]), list[Service])
    assert_type(client.service.inspect("web", pretty=False), Service)
    assert_type(client.service.inspect(["web"], pretty=False), list[Service])
    assert_type(client.service.inspect("web", pretty=True), str)
    assert_type(client.service.inspect(["web", "db"], pretty=True), str)
    assert_type(
        client.service.inspect("web", pretty=pretty), Service | list[Service] | str
    )


def _check_service_ps(client: DockerClient, service: Service) -> None:
    assert_type(client.service.ps("web"), list[Task])
    assert_type(
        client.service.ps(
            ["web"], filters=[("desired-state", "running"), ("node", "n1")]
        ),
        list[Task],
    )
    assert_type(client.service.ps(service, filters=None), list[Task])
    assert_type(service.ps(), list[Task])
    assert_type(service.ps(filters=[("name", "web.1")]), list[Task])
