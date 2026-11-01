# Phone access on a trusted local network

Phone access is an **optional paired browser gateway**. It does not turn the private archive companion into a public server and does not require a cloud account, router port forwarding, or an iCUE installation on the phone.

The PC must stay awake with both the normal companion and the phone gateway running. The phone must be able to reach the PC on the same trusted local network. Guest Wi-Fi/client isolation can prevent this even when both devices use the same router.

## Security boundary

- The normal companion remains bound to `127.0.0.1` with its private core API key.
- The gateway binds only to an explicitly selected private IPv4 address and restricts clients to the selected subnet.
- A random one-time pairing code is shown on the PC. Successful pairing consumes it and generates a new code for the next device.
- Paired phones use a bounded-lifetime HttpOnly, SameSite=Strict session cookie. The phone never receives the core API key in HTML, scripts, media URLs or application state.
- Sessions can be revoked with **Disconnect phone**. Invalid pairing attempts are rate-limited.
- Configuration, databases, source directories, the original generated `widget/defaults.js`, and full ranking exports are not served to the phone.
- Private media and responses use `Cache-Control: no-store`. There is no offline service worker, background vote replay, or private-media cache.

**The LAN connection uses HTTP, not end-to-end encrypted HTTPS.** Pairing prevents unauthenticated browsing but does not protect against someone who can intercept trusted-network traffic. Use this only on trusted home Wi-Fi/LAN. Do not expose this port to the Internet or use it on shared/public Wi-Fi. For encrypted remote access, use a separately configured trusted VPN/HTTPS solution rather than opening the router.

## Find the PC's local address

In PowerShell:

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Select-Object InterfaceAlias, IPAddress, PrefixLength
Get-NetConnectionProfile |
  Select-Object InterfaceAlias, NetworkCategory
```

Choose the active Wi-Fi/Ethernet address, not a VPN/virtual-adapter address, `0.0.0.0`, `127.0.0.1`, or an Internet-facing address. For example, a PC with address `192.168.1.50` and prefix length `24` uses subnet `192.168.1.0/24`. Replace these examples with the actual local address and subnet.

## Start

First start the normal companion as described in [README.md](README.md):

```text
npm start
```

In a second terminal, from the same repository:

```text
npm run phone -- --bind 192.168.1.50 --subnet 192.168.1.0/24 --port 47832
```

The launcher starts the gateway independently with private file logs, prints its URL/code, and exits. It is safe to copy that output or close the viewer; the long-running network process does not use the interactive console. Windows console text selection must not pause pairing or media access.

To display the current code again without interacting with the server process:

```text
npm run phone:code
```

Current owner-only status is stored in ignored `data/phone-access.json`; logs are in ignored `data/phone-gateway.stdout.log` and `data/phone-gateway.stderr.log`. Treat these as private; they are not web endpoints. A code shown earlier may have been consumed and replaced.

On the phone:

1. Connect to the same trusted network.
2. Open the printed URL, for example `http://192.168.1.50:47832/`.
3. Enter the PC's current pairing code.
4. Rank normally. Phone, desktop web view and iCUE use the same saved votes and pending comparison.
5. Tap **Disconnect phone** to revoke that phone's session and return to pairing.

The layout adapts to narrow/touch screens. Retry/Skip remain available for comparisons, video stays muted, and Top 100 uses five-item pages. No private core API key needs to be copied into the phone's settings or address bar.

Stop the gateway with `npm run phone:stop`. The controller verifies that the recorded PID is this project's phone server before stopping it; it does not stop the normal ranker. Restarting invalidates in-memory phone sessions and creates a new pairing code. No automatic startup task is installed. If the PC's DHCP address changes, restart with the new address and update any address-scoped firewall rule/bookmark.

## Windows Firewall

A working loopback test on the PC does **not** prove another device can connect. Windows Firewall may need an inbound rule, and the rule's profile must match the actual Wi-Fi/Ethernet profile.

Prefer a narrowly scoped app rule: the gateway's port, the selected PC IPv4 address, the actual Node executable, and the actual local subnet. Do not disable the firewall, open all ports/profiles, or change a whole network from Public to Private without explicitly approving that broader trust change.

For a network already classified Private, an administrator can use this example after replacing all values:

```powershell
$Node = (Get-Command node.exe).Source
New-NetFirewallRule -Name 'ImageRankerPhone' `
  -DisplayName 'Image Ranker Phone - local subnet only' `
  -Direction Inbound -Action Allow -Protocol TCP `
  -LocalPort 47832 -LocalAddress '192.168.1.50' `
  -RemoteAddress '192.168.1.0/24' -Program $Node -Profile Private
```

If the current network is classified Public, first decide whether it is truly trusted. Keeping the classification unchanged and authorizing only this exact app/subnet/port with a Public-profile rule is a separate explicit security decision. Do not silently change the profile or create an unrestricted Public rule.

To remove a rule you deliberately created under the example name:

```powershell
Remove-NetFirewallRule -Name 'ImageRankerPhone'
```

The firewall does not replace pairing authentication. Never port-forward this service on the router.

## Troubleshooting

- **Login page does not open:** check the printed PC address, gateway terminal, network profile/firewall rule, and router guest/client-isolation settings. Confirm the PC is awake.
- **Code is rejected:** pairing codes are one-time. Read the current code on the PC, not a code used for an earlier session. Repeated incorrect attempts temporarily rate-limit pairing.
- **An older foreground gateway freezes after pairing:** Windows console Select/QuickEdit mode can block a console write when a new code is announced. Use the managed `npm run phone` launcher and `phone:code` viewer instead of running the server interactively; the service's output is redirected to private logs.
- **Chrome on Android fails before opening the login page:** check Android/Chrome local-network permission and try a second browser to distinguish browser policy from server reachability. Do not widen the firewall or disable browser security globally by guesswork. A successful PC self-connection is not inbound-phone evidence.
- **Session expired/disconnected:** private media is cleared and the browser returns to pairing. Enter a fresh code.
- **Desktop works, phone cannot load media:** verify that both processes are running and use the same local `config.json`; do not expose the core key as a workaround.
- **A comparison cannot load:** use Retry or the explicit unrated Skip action, just as on the PC. No source file is deleted.

Automated mobile viewport/security tests do not prove a particular physical phone/router combination. Check the URL on the actual phone after setup.
