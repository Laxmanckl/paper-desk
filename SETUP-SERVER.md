# Phase 2: run the bot on an always-on server

Right now GitHub runs the bot about once an hour (often less: GitHub delays
scheduled runs on free accounts). Phase 2 moves the bot to a small server that
never sleeps:

| | GitHub (Phase 1) | Server (Phase 2) |
| --- | --- | --- |
| Price checks | about hourly, sometimes hours apart | **every minute** |
| Stop-loss / target | caught at the next run | **within a minute** |
| Dashboard | same link | same link, updated every ~15 min |
| If it breaks | red ✗ on the Actions tab | GitHub emails you within the hour |
| Cost | free | AWS Lightsail $5/month, **first 3 months free** |

The bot, the fake $10,000 account and the dashboard link stay the same. Your
open trades carry over. It is still **paper only**: no broker, no real money.

You need about **20 minutes**, a card for AWS sign-up, and nothing on your laptop.

---

## Step 1: Make a GitHub token (lets the server save the account)

1. Open **https://github.com/settings/personal-access-tokens/new**
2. **Token name:** `paper-desk server`
3. **Expiration:** pick **90 days** (or longer; you will redo this step when it expires)
4. **Repository access:** choose **Only select repositories** → select **paper-desk**
5. **Permissions → Repository permissions → Contents:** set to **Read and write**
   (leave everything else as it is)
6. Click **Generate token**, then **copy** the token (starts with `github_pat_`).
   Keep it somewhere safe for Step 3; GitHub shows it only once.

## Step 2: Create the server on AWS Lightsail

1. Go to **https://lightsail.aws.amazon.com** and sign in or create an AWS account
   (needs a card; the plan below is free for 3 months).
2. Click **Create instance**.
3. **Instance location:** any region works. **Mumbai (ap-south-1)** is closest to you.
4. **Platform:** Linux/Unix. **Blueprint:** click **OS Only** → **Ubuntu 24.04 LTS**.
5. **Instance plan:** choose the **$5 USD/month** plan **with IPv4**
   (512 MB RAM is plenty). ⚠️ Do **not** pick the cheaper "IPv6-only" plan:
   GitHub cannot be reached over IPv6, so the bot could not save its account.
6. **Name:** `paper-desk`. Click **Create instance**.
7. Wait about 1 minute until the status says **Running**.

## Step 3: Install the bot (one command)

1. In Lightsail, click the **terminal icon** (`>_`) next to your `paper-desk`
   instance. A black terminal window opens in your browser.
2. Copy this line, paste it into the terminal (right-click → Paste, or Ctrl+Shift+V), press **Enter**:

   ```
   curl -fsSL https://raw.githubusercontent.com/Laxmanckl/paper-desk/main/server/setup.sh | bash
   ```

3. When it asks for the **Token**, paste the token from Step 1 and press **Enter**.
   Nothing appears while you paste; that is normal.
4. Wait 1–2 minutes. It finishes with **"==> Done"** and shows
   `Active: active (running)`.

That's it. You can close the terminal window. The server keeps running.

## Step 4: Check it works

- Within ~15 minutes the dashboard header shows **"Runner: Always-on server ·
  checks every minute"** and "Last check" stays within the last few minutes.
- On GitHub, the **Actions** tab shows runs triggered by "push" (the server
  saving the account) with green ✅.

---

## Everyday use

| You want to… | Do this (in the Lightsail browser terminal) |
| --- | --- |
| Watch the bot live | `journalctl -u paper-desk -f` (Ctrl+C to stop watching) |
| See if it's running | `sudo systemctl status paper-desk` |
| Restart it | `sudo systemctl restart paper-desk` |
| Get the latest code | paste the Step 3 command again |
| Go back to GitHub-only | `bash ~/paper-desk/server/stop.sh` |
| GitHub token expired | `rm ~/.git-credentials`, make a new token (Step 1), paste the Step 3 command again |

**If the server stops:** GitHub still checks every hour that the server has
checked in during the last 45 minutes. If not, that run fails (red ✗) and
GitHub emails you. The dashboard also shows a red dot and a warning. The bot
restarts itself after crashes and after server reboots.

**Cost after 3 months:** $5/month. To stop paying, run `stop.sh` (hands the bot
back to GitHub), then in Lightsail open the instance → **Delete**.
