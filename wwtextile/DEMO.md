# Live demo guide (fictional data only)

Goal: run the ERP on **your computer**, give your client a temporary `https://…` link, let them click through everything, then switch it off.
Nothing here is a production deployment. All companies, people, vehicles and amounts are invented.

## 0. What you need
* Python 3.11+ (developed and tested on 3.13) · Chrome, Edge or Firefox on the computer that runs the demo.
* Optional, for the *Document Reader* only: `tesseract-ocr` (everything else works without it; the demo guide shows it as "Unavailable" if missing).
* A tunnel program for the public link: **cloudflared** (free, no account for quick tunnels).

## 1. Start the demo (3 commands)
```bash
cd wwtextile
python -m pip install -r requirements.txt
python deploy/run_demo.py --reset          # use "python3" on macOS/Linux
```
First run creates `.demo.env` (private, never shown by the app) with a random secret key, a **client password** and a **separate admin password**,
generates about 8 weeks of fictional factory history, and prints the logins:

```
Client logins : ceo, manager1, manager2, store1, store2, prod1, prod2, gate1, gate2, fin1
Client password (share this): …
Admin login   : admin / …   (keep private)
Local address : http://127.0.0.1:8000   health: /healthz
```
Stop it with Ctrl+C. Starting it again **keeps** everything the client entered; `--reset` wipes it and regenerates fresh data (`--seed 7` gives a different story).
Use `--local` instead of nothing/`--reset` only if you will browse over plain `http://127.0.0.1` with Safari (it relaxes the Secure-cookie rule; never use it for the shared link).

## 2. Make the public link
In a **second terminal**:
```bash
# install once:  Windows: winget install --id Cloudflare.cloudflared   ·  macOS: brew install cloudflared   ·  Linux: see developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads
cloudflared tunnel --url http://127.0.0.1:8000
```
It prints a link like `https://random-words.trycloudflare.com`. That is the link to send. It works only while both terminals are running and your computer is awake.
No installer? This works with the `ssh` that ships with Windows 10+, macOS and Linux: `ssh -R 80:127.0.0.1:8000 nokey@localhost.run` (prints an `https://….lhr.life` link).
*(Both tunnel commands could not be run in the development sandbox; they are standard but unverified here, so do step 3 before you send anything.)*

## 3. Verify before you share
```bash
python deploy/verify_demo.py https://random-words.trycloudflare.com --client-password "<client password>" --admin-password "<admin password>" --workflow
```
Passwords are in `.demo.env`. It checks the health endpoint, headers, that anonymous visitors and the wrong/default passwords are rejected, that client users cannot reach admin pages,
that `.demo.env`, the database and source files are not downloadable, that key pages open, **that the admin account cannot sign in through the public link**, and (with `--workflow`) runs the whole factory flow
(receive → issue → record result/reconcile losses → finished stock → dispatch → approve → gate pass → exit → invoice). `--workflow` writes one `VERIFY-…` lot into the demo; run `--reset` afterwards for a clean slate.
Quick manual check: open `<link>/healthz` → `ok`.

## 4. What to send the client
* The link, the **client password**, and the usernames above (start with `ceo`, then `store1` → `manager1` → `prod1` → `manager2` → `gate1` → `fin1` for the factory flow).
* Tell them to open **Demo guide** in the sidebar: it lists the step-by-step flow, what each user can do, and which capabilities are real vs not connected.
* Use a private/second browser window to switch users (one sign-in per browser profile).

## 5. What is real and what is not
| Real (runs here) | Not connected / not built |
|---|---|
| All ERP workflows, validation, maker-checker approvals, ledgers, audit trail, reports, CSV/print, alerts, dashboard charts | Language-model assistant (needs `WW_AI_*` keys; without them only the rule-based engine answers and says so) |
| Rule-based assistant over live data; drafts only, never posts | Server speech-to-text / text-to-speech (needs keys). Browser voice input uses your browser's own recogniser (Chrome/Edge; English; audio may go to the browser vendor) |
| Document Reader (OCR on this machine, if tesseract is installed) – proposes a goods-receipt *draft* | WhatsApp / email / SMS, bank feeds, payment gateways, FBR/tax invoicing, barcode/weighbridge/RFID/CCTV |
The same table is shown live in the app under **Demo guide**.

## 6. Reset and regenerate
* From the terminal: `python deploy/run_demo.py --reset` (or `python -m wwtextile.cli reset-demo --yes --seed 11` with the env from `.demo.env`).
* From the browser: sign in as `admin` **directly at http://127.0.0.1:8000** → Demo guide → Reset (type `RESET`). Everyone is signed out.

## 7. Security model of the shared demo
* Public mode is the default (`--local` turns it off): secure + HttpOnly cookies, 32-byte random secret, per-address sign-in throttle, `noindex` headers and `robots.txt`, `127.0.0.1`-only bind (reachable only through your tunnel).
* The client password is random and different from the admin one. The published default password is rejected. Passwords are never rendered by the app.
* **Admin functions are not exposed publicly:** the `admin` account refuses to sign in through any proxy/tunnel (forwarding headers present), even with the right password, and refused attempts cannot lock it. Client roles have no user-management, settings or reset rights. Override only deliberately with `WW_DEMO_ADMIN_REMOTE=1`.
* The sign-in page is public by necessity: anyone with the link can *try* to sign in. Keep the link private, stop the tunnel when done, and rotate by deleting `.demo.env` and restarting.
* Not suitable for real data: SQLite single node, no backups, no TLS of its own (the tunnel provides it), no external security audit.

## 8. Alternative: a small server instead of your laptop
Same commands on any Ubuntu VPS (`python3 deploy/run_demo.py --reset` under `tmux`/systemd) with Caddy or nginx doing HTTPS in front (`deploy/nginx.conf`). Set `WW_FORWARDED_ALLOW_IPS` to the proxy address. On a VPS you cannot reach `127.0.0.1` from your browser, so use `ssh -L 8000:127.0.0.1:8000 you@server` to open the admin, or the CLI reset.
