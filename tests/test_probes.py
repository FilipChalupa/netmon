"""Cross-platform probe helpers: gateway parsing, ping commands, RTT parsing."""

from netmon_monitor.probes import _parse_gateway, _parse_rtt, _ping_cmd

LINUX_ROUTE = "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n"

DARWIN_ROUTE = """\
   route to: default
destination: default
       mask: default
    gateway: 10.0.0.138
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING>
"""

WINDOWS_ROUTE = """\
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.0.1     192.168.0.42     25
===========================================================================
Persistent Routes:
  None
"""


def test_parse_gateway_linux():
    assert _parse_gateway(LINUX_ROUTE, "Linux") == "192.168.1.1"
    assert _parse_gateway("", "Linux") is None


def test_parse_gateway_darwin():
    assert _parse_gateway(DARWIN_ROUTE, "Darwin") == "10.0.0.138"
    assert _parse_gateway("route: writing to routing socket", "Darwin") is None


def test_parse_gateway_windows():
    assert _parse_gateway(WINDOWS_ROUTE, "Windows") == "192.168.0.1"
    # header row "Network Destination ..." must not match
    assert _parse_gateway("Network Destination Netmask Gateway", "Windows") is None


def test_ping_cmd_per_platform():
    assert _ping_cmd("9.9.9.9", 2.0, "Linux") == \
        ["ping", "-n", "-c", "1", "-W", "2", "9.9.9.9"]
    assert _ping_cmd("9.9.9.9", 2.0, "Darwin") == \
        ["ping", "-n", "-c", "1", "-W", "2000", "9.9.9.9"]
    assert _ping_cmd("9.9.9.9", 2.0, "Windows") == \
        ["ping", "-n", "1", "-w", "2000", "9.9.9.9"]


def test_parse_rtt_unix():
    out = "64 bytes from 9.9.9.9: icmp_seq=1 ttl=58 time=12.3 ms\n"
    assert _parse_rtt(out, "Linux") == 12.3
    assert _parse_rtt("no reply", "Linux") is None


def test_parse_rtt_windows_including_localized():
    assert _parse_rtt("Reply from 9.9.9.9: bytes=32 time=13ms TTL=58", "Windows") == 13.0
    assert _parse_rtt("Reply from 9.9.9.9: bytes=32 time<1ms TTL=58", "Windows") == 1.0
    # Czech Windows
    assert _parse_rtt("Odpověď od 9.9.9.9: bajty=32 čas=8ms TTL=58", "Windows") == 8.0
    assert _parse_rtt("Vypršel časový limit žádosti.", "Windows") is None
