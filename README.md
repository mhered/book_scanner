# Book Scanner MVP

This is a token-free endpoint for uploading QR2CSV exports through Tailscale.
It stores the newest timestamp for each ISBN in a local JSON list.

## ISBN list behavior

The app stores ISBNs and their newest scan timestamps in `isbn_list.json` in
the app directory. The file is ignored by Git. For every upload, it shows:

- **New ISBNs added:** ISBNs that were not already stored.
- **ISBNs updated:** ISBNs whose uploaded timestamp is newer than the stored
  timestamp.
- **Already up to date:** ISBNs whose stored timestamp is the same as or newer
  than the uploaded timestamp.

The CSV must contain `Data` and `Timestamp` columns. QR2CSV timestamps in
`DD/MM/YYYY HH:MM[:SS]` format are supported. If an ISBN appears more than
once in one CSV, only its newest timestamp is processed.

To reset the stored list, stop the service and remove `isbn_list.json`.

## Configuration

Deployment-specific settings are in `.env`, which is ignored by Git. The
current settings file is already present; to create one on another machine,
copy `.env.example` to `.env` and set its values:

```bash
cp .env.example .env
```

- `BOOK_SCANNER_BIND_HOST` is the interface Uvicorn listens on.
- `BOOK_SCANNER_PORT` is the local API port.
- `BOOK_SCANNER_PUBLIC_URL` is the Tailscale URL used to access the server,
  without `/upload`.

## Run on the home server

```bash
set -a
. ./.env
set +a
uv run uvicorn main:app --host "$BOOK_SCANNER_BIND_HOST" --port "$BOOK_SCANNER_PORT"
```

## Run at startup with systemd

The included service runs the API as user `mhered`, starts at boot, and
restarts it after a failure. Stop any manually started Uvicorn process before
enabling it, otherwise the configured port will already be in use. The service
loads its bind host and port from `.env` at startup.

Install and start the service on Ezra:

```bash
cd /home/mhered/apps/book_scanner
sudo install -D -m 0644 systemd/book-scanner.service /etc/systemd/system/book-scanner.service
sudo systemctl daemon-reload
sudo systemctl enable --now book-scanner.service
```

Check that it is active:

```bash
sudo systemctl status book-scanner.service --no-pager
. ./.env
curl --fail-with-body "http://127.0.0.1:${BOOK_SCANNER_PORT}/upload" \
  --request POST \
  --header 'Content-Type: text/csv' \
  --data-binary @/path/to/QR2CSV.csv
```

To view its logs:

```bash
sudo journalctl --unit book-scanner.service --follow
```

After changing [main.py](main.py), restart it:

```bash
sudo systemctl restart book-scanner.service
```

The service is reachable from another device on your tailnet at:

```text
$BOOK_SCANNER_PUBLIC_URL/upload
```

Optionally, configure Tailscale Serve to use an HTTPS hostname instead:

```bash
. ./.env
tailscale serve --https=443 "http://127.0.0.1:${BOOK_SCANNER_PORT}"
```

## Test from another Tailscale device

On another device in the tailnet, upload a CSV and open the HTML result in the default browser:

```bash
result_file="$HOME/Downloads/.book-scanner-result.html"

curl --fail-with-body --silent --show-error \
  --request POST 'http://<server-tailnet-ip>:<port>/upload' \
  --header 'Content-Type: text/csv' \
  --data-binary @'/home/mhered/Desktop/QR_Scans_1791635309.661003.csv' \
  --output "$result_file" \
  && xdg-open "file://$result_file"
```

The response is saved to the hidden file
`~/Downloads/.book-scanner-result.html`, which is replaced on each test. A
hidden file in Downloads is used because Firefox can open it, whereas Firefox
may deny access to files under `~/.cache` or `/tmp`.

## iPhone Shortcut

1. Open **Shortcuts** on the iPhone and create a new shortcut named
   **Upload Book Scans**.
2. Open the shortcut details and enable **Show in Share Sheet**. Some iOS
   versions show an optional **Accepted Types** or **Receive** setting; choose
   **Files** if it is available, otherwise leave the default input setting.
3. Add a **Get Contents of URL** action. Tap the action's **Show More**
   control and configure each field exactly as follows:

   - **URL:** `https://<your-tailnet-IP>/upload`
   - **Method:** tap `GET`, then select `POST`.
   - **Headers:** tap **Add new header** and enter:
     - **Key:** `Content-Type`
     - **Value:** `text/csv`
   - **Request Body:** tap the body-type selector and select `File`.
     Do **not** select `JSON` or `Form`.
   - **File:** tap the blue `File` value in the action, select
     **Select Variable**, then choose **Shortcut Input**.

   The action should read like this:

   ```text
   Get contents of
   https://<your-tailnet-IP>/upload
   Method: POST
   Headers: Content-Type: text/csv
   Request Body: File
   File: Shortcut Input
   ```

4. Add a **Show Content** action after **Get Contents of URL**.
5. Export a CSV from QR2CSV, tap **Share**, and select **Upload ISBN Scans**.

Keep Tailscale connected on the iPhone while uploading. **Show Content**
displays the lists of added, updated, and already up-to-date ISBNs.
Validation failures also display an HTML error page with an explanation and
the appropriate HTTP status code.
