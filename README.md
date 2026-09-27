# Cisco VLAN compliance lab

This small Netmiko program compares Cisco IOS VLANs with `vlans.yaml`. It
collects state and calculates the full plan for one switch before showing that
plan and requesting an explicit `yes`. After approved changes pass post-change
verification, it copies the running configuration to startup configuration.

## Development and testing

This code was created with OpenAI Codex. The script was tested in a lab with
real Cisco Catalyst 2960X and 3850 switches, using VLANs in different statuses.
All lab test runs completed successfully. If you find a bug, please let me know
by opening a GitHub issue.

## Install and prepare

Use a virtual environment, then install the packages listed in
`requirements.txt`. Copy `.env.example` to `.env` and fill in `USER_NAME` and
`PASSWORD`. Copy `switches.example.txt` to `switches.txt`, then add your
reachable lab switch IPs. The local `switches.txt` is ignored by Git so private
lab addresses are not published. Edit
`vlans.yaml` to the VLANs you want managed. Keep the credentials file private;
`.env` is ignored by Git.

The only external packages are Netmiko, PyYAML, and python-dotenv. VLAN output
is parsed from `show vlan brief` by a small local parser; no TextFSM package or
template collection is required. The parser expects the standard IOS table and
fails closed if its header or rows cannot be read confidently. It recognizes
active, suspended, `act/unsup`, `act/lshut`, and related locally or internally
shut statuses. A desired VLAN reported as `act/lshut` is proposed for
activation; other non-active statuses are flagged for manual review.

## Incremental lab test plan

Run the script from this directory with `python vlan_compliance.py`. Start with
one IP in `switches.txt`. Configuration steps require typing the word `yes`;
every other response, including Enter, skips the switch. It saves only when
post-change verification passes; skipped, failed, and already-compliant
switches are not saved.

1. **Load source of truth only:** In a Python prompt, run
   `from vlan_compliance import load_source_of_truth` then
   `print(load_source_of_truth())`. Confirm the IDs are integers and names are
   strings. This import does not connect to a switch.
2. **Discover one switch:** Add one lab IP, set `.env`, and run the script.
   Review the displayed plan; answer `no`. Confirm no configuration changed.
3. **Preview a plan:** Temporarily adjust `vlans.yaml` to create a known
   difference. Run once and answer `no`; check ADD, RENAME, ACTIVATE, REMOVE.
4. **Already compliant:** Set the YAML to match the test switch. Run and
   confirm it reports compliant without prompting.
5. **Controlled addition:** Add one unused test VLAN to YAML, then answer
   `yes`. Check the switch and the post-change verification.
6. **Controlled rename:** Change only that test VLAN's name in YAML; approve
   and verify the new name.
7. **Controlled deletion:** Remove that test VLAN from YAML while leaving it
   present on the switch. Approve only after checking the preview, then verify
   it is gone. Use a disposable test VLAN, never a production VLAN.
8. **Post-change verification:** For a lab check, deliberately cause a
   permitted command to fail or alter the VLAN after configuration and confirm
   the script reports verification failure. Do not perform this on a production
   switch.
9. **Multiple switches:** Only after the single-switch cases above succeed,
   add further lab IPs. Each switch gets its own discovery, preview, and prompt.

Tests 1-4 are read-only if you decline any preview. Keep a backup of the lab
configuration before testing create, rename, or deletion behavior.

## How it is organized

- `load_credentials()` reads environment variables through python-dotenv.
- `load_switches()` reads each non-empty inventory line.
- `load_source_of_truth()` uses `yaml.safe_load()` and validates the VLAN map.
  YAML numeric keys become Python integers; quoted digit strings are converted
  to integers too. The result is a dictionary such as `{10: "USERS"}`.
- `connect_to_device()` opens an SSH session with Netmiko's `cisco_ios` type.
- `get_current_vlans()` runs `show vlan brief` and parses each VLAN row into a
  dictionary such as `{10: {"name": "USERS", "status": "active"}}`. It
  raises an error for unknown output.
- `compare_vlans()` compares dictionary keys, names, and statuses and returns four
  independent change categories (add, rename, activate, remove), along with
  protected VLANs and statuses needing review. It never calls Netmiko or
  applies configuration.
- `display_change_plan()` presents the categories. `confirm_changes()` accepts
  only exact `yes` after trimming whitespace and ignoring letter case.
- `apply_changes()` passes command strings to Netmiko's `send_config_set()`.
  Adds and renames happen before activation and removals. For a locally shut
  VLAN it sends `no shutdown vlan <id>` in global configuration mode.
- The script collects state again and reports each managed VLAN and deletion.
  Only if all managed VLANs pass does `save_running_config()` copy the
  running configuration to startup configuration and check for IOS success.

The comparison uses dictionary membership to identify missing IDs, and value
comparison to detect a name mismatch. The protected ID set is checked first:
VLAN 1 and VLANs 1002-1005 are never added, renamed, or removed. If one of
those IDs appears outside the source of truth, it is shown as protected/ignored.
Desired protected IDs with a different or absent observed state are also
reported as ignored; they cannot make the managed VLAN comparison pass or fail.

Authentication and Netmiko timeout errors are reported per switch. Discovery
and parsing errors stop that switch before confirmation. Command and other
operation errors are also contained to that switch so later switches continue.
The final summary reports each switch status without displaying credentials.

## Before running

The YAML is authoritative for every non-protected VLAN, so any such VLAN not
listed there will be proposed for removal. Check the preview carefully, use a
disposable lab VLAN for deletion tests, and start with one switch. The program
uses the switch's running configuration. Approved changes are copied to
startup configuration only after successful VLAN verification. If verification
fails, the script does not save; if saving itself fails, it reports
`VERIFIED; SAVE FAILED` so you can check the switch before taking further action.

## If Netmiko reports that the prompt pattern was not detected

Netmiko may have timed out while reading a configuration batch, or the switch
may not have returned to the prompt Netmiko expected. The script allows 60
seconds for the configuration batch. If a batch still errors, some commands
may already have been accepted. The script attempts a fresh `show vlan brief`
and prints the observed compliance differences, but it will not retry the
configuration automatically. Review the switch state and the next preview
before approving another attempt.
