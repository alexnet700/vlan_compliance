# Cisco VLAN compliance

This script checks VLANs on Cisco IOS switches and compares them with the list
in `vlans.yaml`. For each switch, it shows the changes it proposes and waits
for your approval before making them.

If you approve, the script applies the changes, checks the VLANs again, and
copies the running configuration to startup configuration only when the checks
pass.

## What the script can change

For VLANs managed by `vlans.yaml`, the script can:

- Add a VLAN that is in the YAML file but missing from the switch.
- Rename a VLAN whose name differs from the YAML file.
- Activate a desired VLAN that has the IOS status `act/lshut`.
- Remove a VLAN from the switch if it is not listed in the YAML file.

VLAN 1 and VLANs 1002–1005 are protected. The script will not add, rename, or
remove them. If a VLAN has another non-active status, such as suspended or
internally shut, the script flags it for manual review and does not try to
change its status.

**Important:** For all other VLANs, `vlans.yaml` is the desired list. A VLAN on
the switch that is missing from the file will be proposed for removal. Review
the full change list before approving, especially before removing VLANs. Start
with one lab switch and a disposable test VLAN.

## Requirements

- Python 3
- SSH access to the Cisco IOS switches
- A switch account with permission to run `show vlan brief` and configure VLANs
- The Python packages in `requirements.txt` (Netmiko, PyYAML, and
  python-dotenv)

## Setup

Clone the repository, then move into the project directory:

```sh
git clone https://github.com/alexnet700/vlan_compliance.git
cd vlan_compliance
```

Run these commands from the project directory:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, activate the environment with `.venv\Scripts\activate`.

Create `.env` and `switches.txt` from their example files:

```sh
cp .env.example .env
cp switches.example.txt switches.txt
```

On Windows, copy the files in File Explorer or use `copy` in Command Prompt.
Then enter the switch login credentials in `.env`:

```text
USER_NAME=your_username
PASSWORD=your_password
```

Put one switch IP address on each line of `switches.txt`. Start with one lab
switch. These local files are ignored by Git, so credentials and lab IP
addresses are not included in commits.

Edit `vlans.yaml` to list the VLAN IDs and names the script should manage. For
example:

```yaml
vlans:
  10:
    name: USERS
  20:
    name: VOICE
```

VLAN names may contain letters, numbers, underscores, and hyphens, and must be
1–32 characters long.

## Run the script

With the virtual environment active and the three files ready, run:

```sh
python vlan_compliance.py
```

For each switch, the script reads `show vlan brief` and displays a plan with
the proposed additions, renames, activations, and removals. Read the plan and
check it against your intended changes.

- Type `yes` to apply the displayed changes to that switch.
- Type anything else, or press Enter, to skip that switch.
- If the switch already matches the YAML file, no changes are needed.

Each switch is handled separately and gets its own preview and approval
prompt. Skipping one switch does not stop the script from processing the next.

## After approval

The script checks the switch again after configuration. It confirms that each
managed VLAN has the expected name and is active, and that planned removals are
gone. If verification succeeds, it copies the running configuration to
startup configuration. If verification fails, it does not save. If the save
fails, the script reports that VLANs were verified but the save failed; check
the switch's startup configuration.

If a configuration command fails, some earlier commands in the batch may
already have taken effect. The script checks the current VLAN state when
possible and does not retry automatically. Inspect the switch and the next
change preview before approving another attempt.

## VLAN output and status handling

The script reads the standard IOS `show vlan brief` table. If the command
fails, the expected table header is missing, or a VLAN row cannot be read
reliably, discovery stops for that switch before any changes are made.

The script can read active, suspended, and several locally or internally shut
statuses. It only automatically activates a desired VLAN with status
`act/lshut`. Other non-active statuses are reported for manual review; the
script does not guess how to fix them.

## Lab testing and feedback

This script was developed with the assistance of OpenAI Codex and tested in a lab environment using Cisco Catalyst 2960X and 3850 switches.

Feel free to use, modify, or adapt it for your own environment. If you encounter any bugs, unexpected behavior, or have suggestions for improvement, please open an issue or let me know.

As always, review and test the script in a lab environment before using it in production.

## Troubleshooting connection or command errors

- **Authentication failed:** Check `USER_NAME` and `PASSWORD` in `.env`.
- **SSH timed out or host is unreachable:** Check the switch IP, SSH access,
  and network reachability.
- **Discovery failed:** Confirm the account can run `show vlan brief` and that
  the switch returned the standard VLAN table.
- **Netmiko did not detect the prompt:** A configuration batch may have
  partially run. Check the switch's VLAN state, then review the next preview
  before approving changes again.
