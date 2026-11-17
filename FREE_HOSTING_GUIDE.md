# Jarvis Maps AI — Free Deployment & Hosting Guide
Complete, zero-cost production setup for website, `.exe` download distribution, and cloud licensing server.

---

## 1. Distribution of the 411 MB `.exe` File (100% Free via GitHub Releases)

> [!NOTE]
> GitHub Git commits reject files over 100 MB, but **GitHub Releases** allows files up to **2.0 GB each** with unlimited free bandwidth through GitHub's global CDN.

1. Go to your GitHub repository: `https://github.com/<your-username>/<your-repo-name>`
2. Click **Releases** (on the right-side panel) ➔ click **Draft a new release**.
3. Fill in:
   - **Tag version**: `v1.1.0`
   - **Release title**: `Jarvis Maps AI v1.1.0 Production Release`
4. In the attachment box labeled *"Attach binaries by dropping them here or selecting them"*, drag and drop:
   - `dist/JarvisMapsAI_Setup_v1.1.exe` (411 MB)
   - *(Optional)* `dist/JarvisMapsAI_Windows_x64.zip`
5. Click **Publish release**.
6. Under the published release, right-click on `JarvisMapsAI_Setup_v1.1.exe` and select **Copy link address**.
   - Your link will look like:  
     `https://github.com/<your-username>/<your-repo-name>/releases/download/v1.1.0/JarvisMapsAI_Setup_v1.1.exe`
7. Paste this URL into `Website/app.js` under `RELEASE_CONFIG.downloadUrl`.

---

## 2. Free Website Hosting (Vercel or GitHub Pages)

### Option A: Vercel (Recommended — Automatic HTTPS & Custom Domain)
1. Go to [vercel.com](https://vercel.com) and sign in with your GitHub account.
2. Click **Add New...** ➔ **Project**.
3. Select your repository.
4. Set **Root Directory** to `Website`.
5. Click **Deploy**.
6. Your website is instantly live at `https://<your-project>.vercel.app` with free SSL!

### Option B: GitHub Pages
1. In your GitHub repository, click **Settings** ➔ **Pages**.
2. Under **Build and deployment** ➔ **Branch**, choose `main` and select `/Website` folder.
3. Click **Save**.
4. Live at `https://<your-username>.github.io/<your-repo-name>/`.

---

## 3. Free Cloud Licensing Server (Render.com)

The repository now contains `render.yaml` ready for one-click free hosting on Render:

1. Create a free account at [render.com](https://render.com).
2. Click **New +** ➔ **Web Service**.
3. Connect your GitHub repository.
4. Render will auto-detect settings from `render.yaml`:
   - **Runtime**: Python 3.11
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server/license_server.py`
   - **Plan**: Free
5. Click **Deploy Web Service**.
6. Render will assign you a live HTTPS URL:
   `https://jarvis-license-server-xxxx.onrender.com`

---

## 4. Connecting the Client App to Your Free Cloud Server

Once your Render web service is live:
1. Open [utils/license_manager.py](file:///c:/Users/User/OneDrive/Desktop/All%20Automations%20Software/Jarvis%20Maps%20AI/utils/license_manager.py).
2. In `get_license_server_urls()`, add your Render URL to the production candidates list:
   ```python
   for fallback in ("https://jarvis-license-server-xxxx.onrender.com", "http://localhost:8081"):
   ```
3. Any user on any computer or network running `Jarvis Maps AI` will now activate online via your free cloud server!
