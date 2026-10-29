"""The pure parts of moving a battery to the local broker.

These build shell commands that run on the gateway, so the validation and the
exact command text are what matter most.
"""
import hashlib

import pytest

from openhomepower.local_broker import (
    ADDON_REPO, MoveError, addon_slug_candidates, addon_version_ok, apply_script,
    hosts_apply_script, hosts_line, hosts_remove_script, is_vendor_broker,
    merge_addon_options,
    parse_redirect_target, redirect_rule, remove_script)


def test_redirect_rule_matches_the_manual_instructions():
    assert redirect_rule("192.168.1.113", 1885) == (
        "iptables -t nat -A OUTPUT -p tcp --dport 1884 -j DNAT "
        "--to-destination 192.168.1.113:1885")


@pytest.mark.parametrize("ip", [
    "192.168.1.113; reboot", "192.168.1.113'", "$(reboot)", "homeassistant.local",
    "", "127.0.0.1", "0.0.0.0", "224.0.0.1", "::1"])
def test_redirect_rule_rejects_anything_but_a_usable_ipv4(ip):
    with pytest.raises(MoveError):
        redirect_rule(ip, 1885)


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_redirect_rule_rejects_bad_ports(port):
    with pytest.raises(MoveError):
        redirect_rule("192.168.1.113", port)


def test_apply_script_backs_up_once_replaces_and_counts():
    s = apply_script(redirect_rule("10.0.0.5", 1885))
    assert '[ -f "$B" ] || cp "$F" "$B"' in s          # backup only if none yet
    assert "sed -i '/--dport 1884 -j DNAT/d'" in s     # old rule(s) out first
    assert "--to-destination 10.0.0.5:1885' >>" in s   # new rule appended
    assert s.endswith("grep -c -- '--dport 1884 -j DNAT' \"$F\"")


def test_remove_script_leaves_the_rest_of_the_file_alone():
    s = remove_script()
    assert "sed -i '/--dport 1884 -j DNAT/d'" in s
    assert "rm " not in s and ">" not in s.split("sed")[0]



def test_hosts_line_pins_the_daemons_hostname():
    assert hosts_line("192.168.1.113") == (
        "192.168.1.113 in.tm.enertek.com.au # openhomepower")


@pytest.mark.parametrize("ip", ["192.168.1.113; reboot", "$(reboot)", "127.0.0.1", ""])
def test_hosts_line_rejects_anything_but_a_usable_ipv4(ip):
    with pytest.raises(MoveError):
        hosts_line(ip)


def test_hosts_apply_script_replaces_our_line_only_and_counts():
    s = hosts_apply_script("10.0.0.5")
    assert s.startswith("F=/etc/hosts; sed -i '/# openhomepower$/d' \"$F\" && ")
    assert "echo '10.0.0.5 in.tm.enertek.com.au # openhomepower' >> \"$F\"" in s
    assert "killall -HUP dnsmasq" in s
    assert s.endswith("grep -c -- '# openhomepower$' \"$F\"")


def test_hosts_remove_script_only_touches_our_line():
    s = hosts_remove_script()
    assert "sed -i '/# openhomepower$/d'" in s
    assert "rm " not in s and ">>" not in s


def test_parse_redirect_target():
    live = "-A OUTPUT -p tcp -m tcp --dport 1884 -j DNAT --to-destination 192.168.1.113:1885"
    saved = ("iptables -t nat -A OUTPUT -p tcp --dport 1884 -j DNAT "
             "--to-destination 192.168.1.50:1885")
    assert parse_redirect_target(f"{live}\n---\n{saved}") == (
        "192.168.1.113:1885", "192.168.1.50:1885")
    assert parse_redirect_target("---\n") == (None, None)
    assert parse_redirect_target("") == (None, None)

@pytest.mark.parametrize("host, vendor", [
    ("in.tm.enertek.com.au", True), ("IN.TM.ENERTEK.COM.AU.", True),
    (" enertek.com.au ", True), ("192.168.1.113", False),
    ("notenertek.com.au", False), ("enertek.com.au.example.org", False), ("", False)])
def test_is_vendor_broker(host, vendor):
    assert is_vendor_broker(host) is vendor


def test_merge_adds_device_and_battery_login():
    new, pw = merge_addon_options(
        {"devices": [], "battery_login": {"username": "", "password": ""}},
        "1234567890", "fw-user", "fw-pass")
    assert new["devices"] == [{"serial": "1234567890", "password": pw}]
    assert new["battery_login"] == {"username": "fw-user", "password": "fw-pass"}
    assert len(pw) >= 20


def test_merge_keeps_an_existing_password_and_other_batteries():
    opts = {"devices": [{"serial": "111", "password": "other"},
                        {"serial": "1234567890", "password": "mine"}],
            "battery_login": {"username": "fw-user", "password": "fw-pass"}}
    new, pw = merge_addon_options(opts, "1234567890", "fw-user", "fw-pass")
    assert pw == "mine"
    assert new["devices"] == opts["devices"]
    assert opts["devices"][1]["password"] == "mine"     # input not mutated


def test_merge_replaces_a_wrong_login_when_no_other_battery_uses_it():
    # e.g. the SSH login typed into the add-on by hand
    new, _ = merge_addon_options(
        {"devices": [{"serial": "1", "password": "p"}],
         "battery_login": {"username": "homepower", "password": "123456"}},
        "1", "fw-user", "fw-pass")
    assert new["battery_login"] == {"username": "fw-user", "password": "fw-pass"}


def test_merge_refuses_a_different_login_another_battery_may_rely_on():
    with pytest.raises(MoveError, match="another battery") as exc:
        merge_addon_options(
            {"devices": [{"serial": "999", "password": "x"}],
             "battery_login": {"username": "someone-else", "password": "secret"}},
            "1", "fw-user", "fw-pass")
    assert "someone-else" in str(exc.value) and "secret" not in str(exc.value)


def test_merge_refuses_an_add_on_too_old_for_battery_login():
    with pytest.raises(MoveError, match="out of date"):
        merge_addon_options({"devices": []}, "1", "fw-user", "fw-pass")


def test_slug_candidates_prefer_what_is_installed():
    repo_hash = hashlib.sha1(ADDON_REPO.lower().encode()).hexdigest()[:8]
    got = addon_slug_candidates(["core_mosquitto", "abcd1234_openhomepower_broker"])
    assert got == ["abcd1234_openhomepower_broker",
                   f"{repo_hash}_openhomepower_broker", "local_openhomepower_broker"]


@pytest.mark.parametrize("version,ok", [
    ("0.2.1", False), ("0.2.0", False), ("0.1.2", False),
    ("0.2.2", True), ("0.2.10", True), ("0.3.0", True), ("1.0", True),
    (None, True), ("dev", True)])
def test_addon_version_gate(version, ok):
    # 0.2.1 exits at start; everything from 0.2.2 works
    assert addon_version_ok(version) is ok
