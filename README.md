# MP4 Studio — GitHub Pages + cloud backend

This version separates the **public HTML website** (GitHub Pages) from the **video-processing backend** (Render or another Docker host). Visitors use the website without installing Python. The downloader uses `yt-dlp[default]`, **Deno**, and **FFmpeg** on the server.

> Only download videos you own or are authorized to download, and comply with the platform's terms. This tool does not bypass DRM, login requirements, paid access, age restrictions, or video removal. Some public videos may fail due to YouTube restrictions or datacenter IP blocking. Deployment does not guarantee that every YouTube video downloads successfully.

## GitHub browser upload: no hidden files required

This package deliberately **does not include any file or folder whose name starts with a dot (`.`)**, so you can upload it using GitHub's **Add file → Upload files** page. The original `.gitignore`, `.dockerignore`, and `.nojekyll` are optional for this deployment, and have been omitted rather than renamed into non-working configuration files. The website and Render backend still use `index.html`, `config.js`, `render.yaml`, `Dockerfile`, and the `backend/` folder.

**Important:** without `.gitignore`, Git will not automatically filter private or generated files. Upload **only the files included in this ZIP**. Do not add `.env` files, private keys, login cookies, passwords, caches, or downloaded videos.

## How to put it online

### 1. Put these files in a GitHub repository

1. Create a **public** GitHub repository, e.g. `mp4-studio`.
2. Upload **the contents of this ZIP's folder**, not the folder itself, to the top level of the repository (`index.html`, `config.js`, `render.yaml`, `Dockerfile`, `backend/`, etc.).
3. Go to **Settings → Pages** in the repository.
4. Under **Build and deployment**, select **Deploy from a branch**, branch `main`, directory **`/(root)`**, then **Save**.
5. Your front end will appear at `https://YOURUSERNAME.github.io/mp4-studio/` (the repository name determines the last path segment). The converter will show **Backend not connected** until the next steps are completed.

### 2. Deploy the backend using Render

1. Go to [render.com](https://render.com) and connect your GitHub account.
2. Choose **New → Blueprint**. Select the same repository. Render reads `render.yaml` and creates the `mp4-studio-api` **Docker** web service.
3. When asked for environment variable `ALLOWED_ORIGINS`, enter **the GitHub Pages origin only**, such as:

   ```text
   https://YOURUSERNAME.github.io
   ```

   **Do not add `/mp4-studio/`** to the origin; browser CORS origins do not include paths. The setting is a comma-separated list if you use multiple domains; keep it restricted to your own site.
4. Deploy it. The build installs Deno, FFmpeg, and Python packages. If the service URL is `https://mp4-studio-api-xxxx.onrender.com`, check `https://mp4-studio-api-xxxx.onrender.com/api/health` for `"ok": true`.
5. You can use Render's Free plan for a hobby/test deployment, subject to its free-tier availability and usage limits. **The free instance sleeps after 15 minutes of inactivity and may take about a minute to wake.** Temporary video files and job status are lost on restart, sleep or redeploy.

If Blueprint setup is unavailable, create a **Web Service** from your GitHub repository, select runtime **Docker** and Free instance if offered, Dockerfile path `./Dockerfile`, and add the `ALLOWED_ORIGINS` environment variable manually.

### 3. Connect the HTML website to Render

Open `config.js` in your GitHub repository, click **Edit**, and set:

```js
window.MP4_STUDIO_API_BASE = "https://mp4-studio-api-xxxx.onrender.com";
```

Replace the example with your real Render HTTPS URL **without a trailing slash**, then commit the change. Reload the GitHub Pages website; it should show **Cloud service online** after the Render backend starts.

While testing, click the **gear icon** in MP4 Studio and enter the Render URL yourself. This saves an override **only in that browser**. To make it work for all visitors, configure `config.js` in GitHub. To clear your override, open the browser console and run `localStorage.removeItem('mp4_studio_backend_override')`, then refresh.

## What is inside?

- `index.html`: standalone static page with URL input, quality selection, progress, and MP4 download link.
- `config.js`: public URL of your hosted backend. **Never put a secret in it.**
- `backend/server.py`: HTTP JSON API, downloader, temporary MP4 delivery, job status, and cleanup.
- `backend/requirements.txt`: yt-dlp and its challenge-solving script dependencies.
- `Dockerfile`: Python + Deno + FFmpeg backend image.
- `render.yaml`: optional Render Blueprint deployment config.
- `tests/test_server.py`: fast local tests using fake download output, no YouTube requests.

## API

- `GET /api/health` — service health check
- `GET /api/info` — version, FFmpeg, Deno and limits
- `POST /api/jobs` — JSON: `{"url":"https://www.youtube.com/watch?v=VIDEO_ID","quality":"720"}`
- `GET /api/jobs/JOB_ID` — progress and status
- `GET /api/jobs/JOB_ID/download` — streaming MP4 download (when ready)

The frontend runs via HTTPS, so the backend should also be served over HTTPS; Render handles the public TLS certificate. `ALLOWED_ORIGINS` must match the page's **origin** exactly (e.g. `https://YOURUSERNAME.github.io`), not its full URL.

## Public-hosting limits and security

- **One active conversion at a time**, with up to three queued/active jobs.
- **Maximum video duration:** 20 minutes by default (`MAX_DURATION_SECONDS=1200`).
- **Maximum final output:** 180 MB by default (`MAX_VIDEO_MB=180`), plus per-stream transfer checks.
- **Basic rate limit:** three requests per ten minutes per perceived client IP. A reverse-proxy/edge limit is recommended for real public traffic; the built-in limit is not an anti-abuse guarantee.
- **Job IDs are hard to guess**, and files expire after approximately 25 minutes (a background cleanup checks every two minutes); a restart can erase them sooner.
- Only normal individual YouTube video/Shorts URLs are accepted. Playlists, arbitrary websites, local files, private authentication cookies, login tools, and DRM circumvention are not supported.
- **CORS is not authentication**. Anyone can send requests directly to a public API, and making a shared downloader public can consume bandwidth/CPU and expose you to unexpected hosting charges or account restrictions. Consider making the backend private or adding real server-side authentication before significant public use.
- Download jobs and files are stored on ephemeral service storage, not retained permanently; there is no user account system or permanent file hosting.

Render docs: https://render.com/docs/free

GitHub Pages docs: https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site

yt-dlp JavaScript runtime guide: https://github.com/yt-dlp/yt-dlp/wiki/EJS

## Test locally (backend)

Install Python 3.11+ plus dependencies (and Deno + FFmpeg to perform actual video downloads), then:

```sh
python -m pip install -r backend/requirements.txt
ALLOWED_ORIGINS=http://localhost:8000 PORT=10000 python backend/server.py
# In a different terminal:
python -m http.server 8000
```

For the local static test at `http://localhost:8000`, click the gear and connect `http://localhost:10000`. To run the included backend tests without installing `yt-dlp`, run:

```sh
python -m unittest discover -s tests -v
```

The tests use a stub downloader and **do not verify live YouTube downloads**. Live downloads require an actual network-enabled deployment, and individual videos can be blocked by YouTube.
