---
sidebar_position: 3
title: Telegram Mini App & Explorer
description: Learn how to manage files using the Telegram Mini App interface.
---

# 📱 Telegram Mini App & Web Explorer

TGDCFS includes a built-in **Telegram Mini App** interface. This web interface allows you to view, explore, and manage your stored files directly from inside Telegram or via a browser.

---

## Accessing the Mini App

The Mini App is a web page served by the TGDCFS frontend, separate from your TGDCFS server (the server does not set up a bot menu button itself). Open it in a browser, or inside Telegram if you configured it as the Mini App of your bot (for example through BotFather).

1. Open the Mini App page.
2. Enter the URL of your TGDCFS server (it must be `https://` when the page itself is served over HTTPS) and your username and password, or choose anonymous access when the server has no users configured.
3. The file explorer loads once the login succeeds.

---

## Features of the Mini App

### 📂 File Explorer
- **Browse Directories**: Navigate through folders in your configured file systems.
- **File Details**: View file size and last modified date for every entry.
- **Download & Upload**: Download files to your device and upload files into the current directory.
- **Folders & Deletion**: Create directories and delete files or folders.

### 🔄 Mirrors & Replication Status
Tap on the **Mirrors and replication** icon in the header bar to inspect the background health of your files:
- **Queue Depth**: See how many files are currently waiting to be mirrored to secondary Telegram or Discord channels.
- **Retry Failed Items**: If a temporary network issue caused a background replication to fail, tap **Retry failed** to immediately attempt copy operations again.
- **Backfill**: Trigger a full scan to ensure every single file has complete copies across all your configured channels.
- **Local Cache Management**: View current local cache usage and tap **Drop unpinned entries** if you need to free up disk space on your server.

### ⚙️ Background Tasks
- Track ongoing uploads, write-back distributions and backfill tasks, with progress and status messages.
- View clear progress percentages and status messages.

---

## Importing Pre-existing Files on Telegram

If you already have files uploaded to a Telegram channel before setting up TGDCFS, you can import them into your file system using the Mini App:

0. The message must be in the Telegram channel that is the primary store of the file system you import into; forward it there first if it is not.
1. Copy the Telegram message link or ID of the file in your channel.
2. Open the Mini App and go to the target directory.
3. Use the **Import** feature to add the file directly into your TGDCFS virtual file system without downloading and re-uploading the data!
