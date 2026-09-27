"""Reconcile Cisco IOS VLANs with a YAML source of truth.

The script discovers and plans for each switch before asking whether to apply
that switch's plan. It saves only after all managed VLANs verify successfully.
"""

import os
import re
import sys

import yaml
from dotenv import load_dotenv
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)


SOURCE_FILE = "vlans.yaml"
SWITCH_FILE = "switches.txt"

# Never modify these Cisco default/reserved VLANs. Add IDs here if your lab
# has other VLANs that must be protected.
PROTECTED_VLANS = {1, 1002, 1003, 1004, 1005}


class DiscoveryError(Exception):
    """Raised when switch output cannot be trusted for planning changes."""


def load_credentials():
    """Load the login values from .env and return them as a dictionary."""
    load_dotenv()
    username = os.getenv("USER_NAME")
    password = os.getenv("PASSWORD")

    if not username or not password:
        raise ValueError("USER_NAME and PASSWORD must both be set in .env")

    return {"username": username, "password": password}


def load_switches(filename=SWITCH_FILE):
    """Read non-empty switch IP address lines from the inventory file."""
    with open(filename, "r", encoding="utf-8") as inventory_file:
        switches = [line.strip() for line in inventory_file if line.strip()]

    if not switches:
        raise ValueError(f"No switch IP addresses found in {filename}")
    return switches


def normalize_vlan_id(value):
    """Convert an integer or digit-only YAML key to a valid VLAN ID."""
    if isinstance(value, bool):
        raise ValueError("Boolean values are not valid VLAN IDs")
    if isinstance(value, int):
        vlan_id = value
    elif isinstance(value, str) and value.isdigit():
        vlan_id = int(value)
    else:
        raise ValueError(f"Malformed VLAN ID: {value!r}")

    if not 1 <= vlan_id <= 4094:
        raise ValueError(f"VLAN ID {vlan_id} must be between 1 and 4094")
    return vlan_id


def load_source_of_truth(filename=SOURCE_FILE):
    """Load YAML and return a simple {integer_vlan_id: vlan_name} dictionary."""
    with open(filename, "r", encoding="utf-8") as source_file:
        try:
            document = yaml.safe_load(source_file)
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid YAML in {filename}: {error}") from error

    if not isinstance(document, dict) or not isinstance(document.get("vlans"), dict):
        raise ValueError(f"{filename} must contain a 'vlans' mapping")

    desired_vlans = {}
    for raw_vlan_id, vlan_details in document["vlans"].items():
        vlan_id = normalize_vlan_id(raw_vlan_id)
        if vlan_id in desired_vlans:
            raise ValueError(f"VLAN ID {vlan_id} is listed more than once")
        if not isinstance(vlan_details, dict):
            raise ValueError(f"VLAN {vlan_id} must have a mapping with a name")

        name = vlan_details.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ValueError(
                f"VLAN {vlan_id} needs a 1-32 character name using letters, "
                "numbers, underscores, or hyphens"
            )
        desired_vlans[vlan_id] = name

    return desired_vlans


def connect_to_device(ip_address, credentials):
    """Open an SSH connection to one Cisco IOS switch."""
    return ConnectHandler(
        device_type="cisco_ios",
        host=ip_address,
        username=credentials["username"],
        password=credentials["password"],
        conn_timeout=10,
        auth_timeout=10,
        banner_timeout=15,
    )


def get_current_vlans(connection):
    """Run show vlan brief and return VLAN IDs mapped to name/status details.

    A familiar header and at least one VLAN row are required. Each entry stores
    both its name and IOS status. If a line appears to start a VLAN row but
    cannot be parsed, discovery fails closed.
    """
    output = connection.send_command("show vlan brief", read_timeout=30)
    if not isinstance(output, str) or not output.strip():
        raise DiscoveryError("show vlan brief returned no output")
    if re.search(r"%\s*(Invalid|Incomplete|Ambiguous|Authorization failed)", output, re.I):
        raise DiscoveryError("The switch rejected 'show vlan brief'")

    lines = output.splitlines()
    has_header = any(re.search(r"\bVLAN\s+Name\s+Status\b", line, re.I) for line in lines)
    if not has_header:
        raise DiscoveryError("Could not find the VLAN/Name/Status table header")

    actual_vlans = {}
    # Keep status alongside the name: act/lshut means the VLAN exists but is
    # locally shut, so a desired VLAN can be explicitly reactivated.
    row_pattern = re.compile(
        r"^\s*(\d+)\s+(\S+)\s+"
        r"(active|suspend(?:ed)?|act/unsup|act/lshut|sus/lshut|act/ishut|sus/ishut)\b",
        re.I,
    )
    for line in lines:
        match = row_pattern.match(line)
        if match:
            vlan_id = int(match.group(1))
            if not 1 <= vlan_id <= 4094:
                raise DiscoveryError(f"Switch output contains invalid VLAN ID {vlan_id}")
            if vlan_id in actual_vlans:
                raise DiscoveryError(f"Switch output repeats VLAN ID {vlan_id}")
            actual_vlans[vlan_id] = {
                "name": match.group(2),
                "status": match.group(3).lower(),
            }
        elif re.match(r"^\s*\d+\s+\S+", line) and not re.match(
            r"^\s*(VLAN|----)\b", line, re.I
        ):
            raise DiscoveryError(f"Could not parse possible VLAN row: {line.strip()!r}")

    if not actual_vlans:
        raise DiscoveryError("No VLAN rows were parsed from switch output")
    return actual_vlans


def compare_vlans(actual_vlans, desired_vlans):
    """Build a change plan only; this function never communicates with a switch."""
    plan = {
        "add": {},
        "remove": {},
        "rename": {},
        "activate": {},
        "status_review": {},
        "protected": {},
    }

    for vlan_id in sorted(PROTECTED_VLANS):
        actual_vlan = actual_vlans.get(vlan_id)
        actual_name = actual_vlan["name"] if actual_vlan else None
        desired_name = desired_vlans.get(vlan_id)
        if actual_name is not None and desired_name is None:
            plan["protected"][vlan_id] = actual_name
        elif desired_name is not None and actual_name != desired_name:
            plan["protected"][vlan_id] = actual_name or f"absent (desired {desired_name})"

    for vlan_id, desired_name in desired_vlans.items():
        if vlan_id in PROTECTED_VLANS:
            continue
        if vlan_id not in actual_vlans:
            plan["add"][vlan_id] = desired_name
        else:
            actual_vlan = actual_vlans[vlan_id]
            if actual_vlan["name"] != desired_name:
                plan["rename"][vlan_id] = (actual_vlan["name"], desired_name)
            if actual_vlan["status"] == "act/lshut":
                plan["activate"][vlan_id] = desired_name
            elif actual_vlan["status"] != "active":
                # Do not guess how to clear suspended or internally shut states.
                plan["status_review"][vlan_id] = actual_vlan["status"]

    for vlan_id, actual_vlan in actual_vlans.items():
        if vlan_id not in desired_vlans and vlan_id not in PROTECTED_VLANS:
            plan["remove"][vlan_id] = actual_vlan["name"]

    return plan


def display_change_plan(switch_name, ip_address, plan):
    """Print all proposed changes for one switch before confirmation."""
    print("\n" + "=" * 50)
    print(f"Switch: {switch_name} - {ip_address}")
    print("=" * 50)

    if not (
        plan["add"]
        or plan["rename"]
        or plan["remove"]
        or plan["activate"]
        or plan["status_review"]
    ):
        print(f"{switch_name} is compliant. No changes required.")
    else:
        for heading, key in (
            ("ADD", "add"),
            ("RENAME", "rename"),
            ("ACTIVATE", "activate"),
            ("REMOVE", "remove"),
        ):
            print(f"\n{heading}:")
            if not plan[key]:
                print("  (none)")
            elif key == "rename":
                for vlan_id, (old_name, new_name) in sorted(plan[key].items()):
                    print(f"  VLAN {vlan_id}: {old_name} -> {new_name}")
            else:
                for vlan_id, name in sorted(plan[key].items()):
                    print(f"  VLAN {vlan_id} - {name}")

    print("\nProtected/ignored:")
    if plan["protected"]:
        for vlan_id, name in sorted(plan["protected"].items()):
            print(f"  VLAN {vlan_id} - {name}")
    else:
        print("  (none)")

    if plan["status_review"]:
        print("\nStatus needs manual review (no automatic status command will be sent):")
        for vlan_id, status in sorted(plan["status_review"].items()):
            print(f"  VLAN {vlan_id} - {status}")

    if plan["add"] or plan["rename"] or plan["remove"] or plan["activate"]:
        print("\nNo configuration has been changed.")


def confirm_changes(switch_name):
    """Require the exact word 'yes'; blank input always declines."""
    answer = input(f"Apply these changes to {switch_name}? [yes/no]: ")
    return answer.strip().lower() == "yes"


def apply_changes(connection, plan):
    """Send planned VLAN configuration commands without saving the config."""
    commands = []
    for vlan_id, name in sorted(plan["add"].items()):
        commands.extend([f"vlan {vlan_id}", f"name {name}", "exit"])
    for vlan_id, (_old_name, new_name) in sorted(plan["rename"].items()):
        commands.extend([f"vlan {vlan_id}", f"name {new_name}", "exit"])
    for vlan_id in sorted(plan["activate"]):
        # IOS global configuration command that removes a local VLAN shutdown.
        commands.append(f"no shutdown vlan {vlan_id}")
    for vlan_id in sorted(plan["remove"]):
        commands.append(f"no vlan {vlan_id}")

    if commands:
        # A multi-VLAN batch can outlast Netmiko's short default timeout on
        # slower lab links. IOS returns a prompt after each config command.
        output = connection.send_config_set(commands, read_timeout=60)
        if re.search(r"%\s*(Invalid|Incomplete|Ambiguous|Error|Failed)", output, re.I):
            raise RuntimeError("The switch reported an error while applying configuration")


def save_running_config(connection):
    """Copy running-config to startup-config after successful verification."""
    output = connection.send_command_timing(
        "copy running-config startup-config",
        read_timeout=60,
    )

    # IOS normally asks to confirm the default destination filename. Pressing
    # Enter accepts startup-config. Timing-based reads avoid relying on a
    # config-mode prompt during this save dialogue.
    if "destination filename" in output.lower():
        output += connection.send_command_timing("", read_timeout=60)

    if re.search(r"%\s*(Invalid|Incomplete|Ambiguous|Error|Failed)", output, re.I):
        raise RuntimeError("The switch reported an error while saving the configuration")
    if not re.search(r"(\[OK\]|bytes copied|copy complete|configuration saved)", output, re.I):
        raise RuntimeError("The switch did not confirm that the configuration was saved")


def display_verification(actual_vlans, desired_vlans):
    """Print managed VLAN checks and report whether managed state matches."""
    differences = compare_vlans(actual_vlans, desired_vlans)
    print("\nVerification:")

    check_ids = sorted(
        vlan_id for vlan_id in desired_vlans if vlan_id not in PROTECTED_VLANS
    )
    for vlan_id in check_ids:
        expected_name = desired_vlans[vlan_id]
        actual_vlan = actual_vlans.get(vlan_id)
        actual_name = actual_vlan["name"] if actual_vlan else None
        actual_status = actual_vlan["status"] if actual_vlan else None
        if actual_name == expected_name and actual_status == "active":
            print(f"VLAN {vlan_id} {expected_name}  PASS")
        elif actual_name is None:
            print(f"VLAN {vlan_id} {expected_name}  FAIL (missing)")
        elif actual_name != expected_name:
            print(f"VLAN {vlan_id} expected {expected_name}, found {actual_name}  FAIL")
        else:
            print(
                f"VLAN {vlan_id} {expected_name}  FAIL "
                f"(status: {actual_status}; expected active)"
            )

    for vlan_id, old_name in sorted(differences["remove"].items()):
        print(f"VLAN {vlan_id} {old_name} removed  FAIL (still present)")

    compliant = (
        not differences["add"]
        and not differences["rename"]
        and not differences["remove"]
        and not differences["activate"]
        and not differences["status_review"]
    )
    print(f"\nRESULT: {'COMPLIANT' if compliant else 'VERIFICATION FAILED'}")
    return compliant


def process_switch(ip_address, switch_number, credentials, desired_vlans):
    """Discover, preview, optionally configure, and verify one switch."""
    switch_name = f"SW{switch_number:02d}"
    connection = None
    try:
        connection = connect_to_device(ip_address, credentials)
        actual_vlans = get_current_vlans(connection)
        plan = compare_vlans(actual_vlans, desired_vlans)
        display_change_plan(switch_name, ip_address, plan)

        if not (
            plan["add"] or plan["rename"] or plan["remove"] or plan["activate"]
        ):
            if plan["status_review"]:
                print(f"\n{switch_name}: VLAN status requires manual review; no changes applied.")
                return "STATUS NEEDS REVIEW"
            return "ALREADY COMPLIANT"
        if not confirm_changes(switch_name):
            return "USER SKIPPED"

        try:
            apply_changes(connection, plan)
        except Exception as error:
            # A command batch can fail after some earlier commands succeeded.
            # Read state again if possible; never retry automatically.
            print(
                f"\n{switch_name}: configuration command failed: {error}\n"
                "Some earlier commands may have been applied. Checking current VLAN state."
            )
            try:
                current_vlans = get_current_vlans(connection)
                display_verification(current_vlans, desired_vlans)
            except Exception as verification_error:
                print(
                    f"{switch_name}: unable to verify after the command failure: "
                    f"{verification_error}"
                )
            return "CONFIGURATION FAILED"

        verified_vlans = get_current_vlans(connection)
        print("\nDeletion checks:")
        for vlan_id, name in sorted(plan["remove"].items()):
            result = "PASS" if vlan_id not in verified_vlans else "FAIL"
            print(f"VLAN {vlan_id} {name} removed  {result}")
        if display_verification(verified_vlans, desired_vlans):
            try:
                save_running_config(connection)
            except Exception as error:
                print(f"\n{switch_name}: VLANs verified, but saving failed: {error}")
                return "VERIFIED; SAVE FAILED"
            print("\nRunning configuration copied to startup configuration.")
            return "CHANGED + VERIFIED + SAVED"
        return "VERIFICATION FAILED"
    except NetmikoAuthenticationException:
        print(f"\n{switch_name} ({ip_address}): authentication failed")
        return "CONNECTION FAILED"
    except NetmikoTimeoutException:
        print(f"\n{switch_name} ({ip_address}): SSH connection timed out or host is unreachable")
        return "CONNECTION FAILED"
    except DiscoveryError as error:
        print(f"\n{switch_name} ({ip_address}): discovery failed; no changes made: {error}")
        return "DISCOVERY FAILED"
    except (OSError, RuntimeError, ValueError) as error:
        print(f"\n{switch_name} ({ip_address}): operation failed: {error}")
        return "OPERATION FAILED"
    except Exception as error:
        # Keep one device's unexpected failure from stopping later switches.
        print(f"\n{switch_name} ({ip_address}): unexpected error: {error}")
        return "OPERATION FAILED"
    finally:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception as error:
                # A disconnect problem should be visible without stopping the
                # inventory loop or printing connection details/credentials.
                print(f"\n{switch_name} ({ip_address}): error closing SSH session: {error}")


def main():
    """Load inputs once, then process switches independently."""
    try:
        desired_vlans = load_source_of_truth()
        switches = load_switches()
        credentials = load_credentials()
    except (OSError, ValueError) as error:
        print(f"Startup failed: {error}")
        return 1

    results = []
    for switch_number, ip_address in enumerate(switches, start=1):
        status = process_switch(ip_address, switch_number, credentials, desired_vlans)
        results.append((f"SW{switch_number:02d}", status))

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for switch_name, status in results:
        print(f"{switch_name:<8} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
