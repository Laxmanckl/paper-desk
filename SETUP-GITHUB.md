# Put the paper-trading bot online (no laptop needed)

When this is set up, GitHub's computers run the bot **every hour, for free**,
using real gold and EUR/USD prices and a **fake $10,000 account**. You watch it
on a web page that works on your phone:

    https://YOUR-GITHUB-NAME.github.io/REPO-NAME/

Your laptop can be off. Setup takes about 10 minutes, all in the browser.

---

## Step 1: Create a GitHub account
Go to **github.com** and sign up (free). Skip this if you already have one.

## Step 2: Create a repository
1. Click **+** (top right) → **New repository**.
2. Name: `paper-desk` (any name works; it becomes part of your dashboard link).
3. Choose **Public**.
   *Free GitHub Pages hosting needs a public repository. Anyone with the link
   could see the fake trades, but there is no personal or money data in it.*
4. Click **Create repository**.

## Step 3: Upload the files
1. On the new, empty repository page, click **"uploading an existing file"**.
2. Unzip `jev-bot-forex.zip` on your computer, open the `jev-bot` folder, select
   **everything inside it**, and drag it into the upload box.
3. Click **Commit changes**.

**Check:** the repository should now show a folder called **`.github`**.
Mac and some Windows setups hide folders that start with a dot, so they may not
have been uploaded. If `.github` is missing:
1. Click **Add file → Create new file**.
2. In the file name box type exactly: `.github/workflows/paper-trade.yml`
3. Open `paper-trade.yml` from the zip (inside `.github/workflows/`) in Notepad or
   TextEdit, copy everything, paste it into GitHub, and click **Commit changes**.

## Step 4: Turn on the web page
1. In the repository, open **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **GitHub Actions**.

## Step 5: Allow the bot to save its account
1. **Settings → Actions → General**.
2. Scroll to **Workflow permissions**, choose **Read and write permissions**,
   and click **Save**.

## Step 6: Start it
1. Open the **Actions** tab. If GitHub asks, click to enable workflows.
2. Click **paper-trade** on the left → **Run workflow** → **Run workflow**.
3. Wait 1–2 minutes for the green tick ✅.
4. Open your dashboard: **Settings → Pages** shows the link, which looks like
   `https://YOUR-GITHUB-NAME.github.io/paper-desk/`. Bookmark it on your phone.

From now on it runs by itself every hour. The dashboard refreshes itself every
5 minutes while it is open.

---

## Everyday use

| You want to… | Do this |
| --- | --- |
| See how it is doing | Open your dashboard link |
| Run a check right now | Actions → paper-trade → Run workflow |
| Pause the bot | Actions → paper-trade → `···` → Disable workflow |
| Resume | Same menu → Enable workflow |
| Start over with $10,000 | Open `state/paper_account.json` → 🗑 delete → Commit, then Run workflow |
| Use spot gold prices (optional) | Get a free key at twelvedata.com, then Settings → Secrets and variables → Actions → New secret, name `TWELVEDATA_API_KEY`. The bot switches to Twelve Data automatically. |

## If something goes wrong

- **The dashboard says "No check in the last N hours"**: on weekends this is
  normal, because forex is closed. Otherwise open the **Actions** tab. A red ✗
  means a run failed; click it to see the message.
- **A run failed with "data error"** in its log: Yahoo was briefly unavailable.
  It retries on the next hourly run by itself.
- **A run failed at "Save the account" with a permission error**: redo Step 5.
- **The dashboard link shows 404**: redo Step 4, then run the workflow once more.
- **Runs start a few minutes late**: GitHub delays scheduled runs when it is busy.
  For a once-a-day strategy this does not matter.

## Good to know

- **Free:** public repositories get free GitHub Actions minutes and free Pages hosting.
- **Fake money only.** There is no broker connection in this code, so it cannot
  place a real trade.
- **Every check is saved** as a commit, so the repository's history is a full log
  of what the bot did and when.
- Run it for **2–3 months** before drawing conclusions. It makes one decision per
  trading day.
