# PhoneBridge

Your Linux phone (FuriOS, Phosh, Mobian …) in the panel of your desktop.

- **Panel icon** (StatusNotifierItem – XFCE "Status Tray", KDE, Cinnamon, MATE, waybar):
  battery level as a filling phone, a bolt while charging, a badge with unread SMS,
  details in the tooltip, a menu for the rest.
- **Window** (GTK 4 / libadwaita), opened from the icon:
  - *Overview*: battery, mobile network, Wi-Fi; mobile data, Wi-Fi, volume,
    ring profile, power profile, "ring the phone" to find it
  - *Messages*: chatty's SMS conversations, reply, write new ones, call from the phone;
    a desktop notification for every new SMS
  - *Phone settings*: common GNOME settings as switches, every other GSettings key
    through a search
- Several phones, one of them shown in the panel.

## How it works

Nothing is installed on the phone and no port is opened there. PhoneBridge starts
`python3` on the phone over SSH and feeds it `phonebridge/agent.py`; the agent answers
JSON lines on the same connection and reports changes by itself (UPower, ofono, chatty's
store). It needs Python 3 and PyGObject on the phone – both are there on FuriOS/Phosh.

| What | Source on the phone |
|---|---|
| Battery | UPower |
| Network, mobile data, incoming SMS | ofono |
| Sending SMS | ModemManager (like chatty) |
| SMS history | chatty's SQLite store, read only |
| Wi-Fi | nmcli |
| Volume | wpctl |
| Ring profile, ringing | feedbackd |
| Power profile | power-profiles (batman) |
| Settings | GSettings |

Messages sent from PhoneBridge are kept in `~/.local/share/phonebridge/sent.jsonl`
on the phone, because chatty does not list messages it did not send itself.

## Install

```sh
ssh-copy-id furios@<phone>      # once, if not done yet
./install.sh                    # into ~/.local, starts at login
phonebridge                     # or from the menu
```

Needs GTK 4, libadwaita, PyGObject and pycairo (Manjaro/Arch:
`pacman -S python-gobject libadwaita python-cairo`). Phones are added under
*Menu → Phones …*.

## Tests

```sh
tests/run-tests.sh
```

No phone, no network, no root: the agent runs locally against a made-up chatty
store, on a private session bus.
