#!/usr/bin/env python3
"""Sincronización idempotente de inventario NetBox (SSoT) hacia Zabbix vía API."""

import os
import sys
import requests


def get_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        print(f"[ERROR] Variable de entorno faltante: {var_name}", file=sys.stderr)
        sys.exit(1)
    return value


class ZabbixClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.headers = {
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {token}",
        }
        self._req_id = 1

    def call(self, method: str, params: dict) -> dict | list:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._req_id,
        }
        self._req_id += 1
        resp = requests.post(self.url, json=payload, headers=self.headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Zabbix API error en {method}: {data['error']}")
        return data["result"]


def fetch_netbox_devices(nb_url: str, nb_token: str) -> list[dict]:
    url = f"{nb_url.rstrip('/')}/api/dcim/devices/"
    headers = {
        "Authorization": f"Token {nb_token}",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, params={"status": "active"}, timeout=15)
    resp.raise_for_status()
    devices = []
    for dev in resp.json().get("results", []):
        primary_ip = dev.get("primary_ip4") or dev.get("primary_ip")
        if not primary_ip:
            continue
        ip_addr = primary_ip["address"].split("/")[0]
        devices.append({
            "name": dev["name"],
            "ip": ip_addr,
            "role": dev.get("role", {}).get("slug", "router"),
            "site": dev.get("site", {}).get("name", "DC-LAB"),
        })
    return devices


def ensure_hostgroup(zbx: ZabbixClient, group_name: str) -> str:
    groups = zbx.call("hostgroup.get", {"filter": {"name": [group_name]}})
    if groups:
        return groups[0]["groupid"]
    created = zbx.call("hostgroup.create", {"name": group_name})
    return created["groupids"][0]


def find_cisco_snmp_template(zbx: ZabbixClient) -> str:
    candidates = ["Cisco IOS by SNMP", "Cisco IOS SNMP"]
    templates = zbx.call("template.get", {"filter": {"host": candidates}})
    if not templates:
        templates = zbx.call("template.get", {"search": {"host": "Cisco IOS"}, "searchWildcardsEnabled": True})
    if not templates:
        raise RuntimeError("No se encontró ningún template 'Cisco IOS by SNMP' en Zabbix.")
    return templates[0]["templateid"]


def sync_hosts(zbx: ZabbixClient, devices: list[dict], group_id: str, template_id: str, community: str) -> None:
    for dev in devices:
        existing = zbx.call("host.get", {"filter": {"host": [dev["name"]]}, "selectInterfaces": ["interfaceid"]})
        snmp_interface = {
            "type": 2,
            "main": 1,
            "useip": 1,
            "ip": dev["ip"],
            "dns": "",
            "port": "161",
            "details": {
                "version": 2,
                "bulk": 1,
                "community": "{$SNMP_COMMUNITY}",
            },
        }
        macros = [{"macro": "{$SNMP_COMMUNITY}", "value": community}]

        if not existing:
            zbx.call("host.create", {
                "host": dev["name"],
                "name": dev["name"],
                "groups": [{"groupid": group_id}],
                "templates": [{"templateid": template_id}],
                "interfaces": [snmp_interface],
                "macros": macros,
                "tags": [
                    {"tag": "site", "value": dev["site"]},
                    {"tag": "role", "value": dev["role"]},
                    {"tag": "managed_by", "value": "netbox-ssot"},
                ],
            })
            print(f"[CREATED] {dev['name']} ({dev['ip']}) registrado en Zabbix.")
        else:
            host_id = existing[0]["hostid"]
            zbx.call("host.update", {
                "hostid": host_id,
                "groups": [{"groupid": group_id}],
                "templates": [{"templateid": template_id}],
                "macros": macros,
            })
            print(f"[UPDATED] {dev['name']} ({dev['ip']}) actualizado en Zabbix (hostid={host_id}).")


def main() -> None:
    nb_url = get_env("NETBOX_URL")
    nb_token = get_env("NETBOX_TOKEN")
    zbx_url = get_env("ZABBIX_URL")
    zbx_token = get_env("ZABBIX_TOKEN")
    community = os.getenv("SNMP_COMMUNITY", "ISP_NOC_RO")

    devices = fetch_netbox_devices(nb_url, nb_token)
    if not devices:
        print("[WARN] No se encontraron dispositivos activos con IP primaria en NetBox.")
        return

    zbx = ZabbixClient(zbx_url, zbx_token)
    group_id = ensure_hostgroup(zbx, "ISP-Routers")
    template_id = find_cisco_snmp_template(zbx)
    sync_hosts(zbx, devices, group_id, template_id, community)


if __name__ == "__main__":
    main()
