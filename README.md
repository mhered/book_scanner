# Book Scanner MVP

This is a token-free service for assigning storage locations to QR2CSV book
scans through Tailscale. The iPhone shortcut uploads a CSV, then opens a
mobile-friendly page where the batch location is selected before it is saved.

## ISBN list behavior

The app stores ISBN records in `isbn_list.json` in the app directory. The file
is ignored by Git. Each record stores its newest scan timestamp and its
location ID; the ISBN itself is the JSON object key. `locations.json` is a
human-editable, versioned tree configuration containing each location's ID,
name, and parent ID.

For every confirmed upload, it shows:

- **New ISBNs added:** ISBNs that were not already stored.
- **ISBNs updated:** ISBNs whose uploaded timestamp is newer than the stored
  timestamp, including any ISBN whose stored timestamp is unknown, or whose
  timestamp is the same but the selected location is different.
- **Already up to date:** ISBNs whose uploaded timestamp is older, whose
  timestamp and location are unchanged, or whose incoming timestamp is unknown.

To correct a location selected by mistake, upload the same CSV again and select
the correct location. A matching valid timestamp allows the location to change;
a strictly older timestamp cannot overwrite the stored timestamp or location.
Missing or invalid timestamps do not overwrite existing records.

The CSV must contain `Data` and `Timestamp` columns. QR2CSV timestamps in
`MM/DD/YYYY HH:MM[:SS]` format are supported. Missing or malformed timestamps
are saved as an unknown timestamp and can be replaced by any later valid
timestamp. Rows without an ISBN are skipped and shown in the result. If an
ISBN appears more than once in one CSV, its newest valid timestamp is used.

## Configuration

Deployment-specific settings are in `.env`, which is ignored by Git. The
current settings file is already present; to create one on another machine,
copy `.env.example` to `.env` and set its values:

```bash
cp .env.example .env
```

- `BOOK_SCANNER_BIND_HOST` is the interface Uvicorn listens on, e.g. 0.0.0.0
- `BOOK_SCANNER_PORT` is the local API port, e.g. 8082
- `BOOK_SCANNER_PUBLIC_URL` is the Tailscale URL used to access the server,
  without `/upload`, e.g. http://100.106.249.52:8082

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
batch_url="$(curl --fail-with-body --silent --show-error \
  "http://127.0.0.1:${BOOK_SCANNER_PORT}/upload" \
  --request POST \
  --header 'Content-Type: text/csv' \
  --data-binary @/path/to/QR2CSV.csv)"
printf '%s\n' "$batch_url"
```

Open the returned URL in a browser to select or create the batch location.

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

On another device in the tailnet, upload a CSV and open the returned
location-assignment page in the default browser:

```bash
batch_url="$(curl --fail-with-body --silent --show-error \
  --request POST 'http://<server-tailnet-ip>:<port>/upload' \
  --header 'Content-Type: text/csv' \
  --data-binary @'/path/to/csv')"
xdg-open "$batch_url"
```

## iPhone Shortcut

The shortcut needs only two actions: one to upload the CSV and one to open
the location-assignment URL returned by the server.

### 1. Connect Tailscale

Open **Tailscale** on the iPhone and make sure it is connected. Keep it
connected throughout the upload and location-assignment steps. Note the Tailscale URL used to access the server.

### 2. Create the shortcut

1. Open **Shortcuts** and tap **+**.
2. Name the shortcut **Upload ISBN Scans**.
3. Open the shortcut's **Details** and enable **Show in Share Sheet**.
4. If there is an **Accepted Types** or **Receive** setting, choose **Files**
   if available; otherwise leave the default input setting.

### 3. Add the upload action

1. Tap **Add Action**, search for **Get Contents of URL**, and add it.
2. Enter the upload URL, e.g.:

   ```text
   http://100.106.249.52:8082/upload
   ```

   On another deployment, use `BOOK_SCANNER_PUBLIC_URL` followed by `/upload`.
   The current direct server uses **HTTP, not HTTPS**. Use HTTPS only if you
   have configured Tailscale Serve or another HTTPS endpoint.
3. Expand the action using its arrow or **Show More** control.
4. Change **Method** from `GET` to **POST**.
5. Under **Headers**, tap **Add new header** and enter:

   | Field | Value |
   |---|---|
   | Key | `Content-Type` |
   | Value | `text/csv` |

6. Set **Request Body** to **File**, not JSON or Form.
7. Tap the **File** input, choose **Select Variable**, and select
   **Shortcut Input**.

The action should look like this:

```text
Get Contents of URL
  URL: http://100.106.249.52:8082/upload
  Method: POST
  Headers:
    Content-Type: text/csv
  Request Body: File
  File: Shortcut Input
```

### 4. Add the browser-opening action

1. Below the upload action, tap **Add Action**.
2. Search for **Open URLs** and add it.
3. Tap its URL input and choose **Select Variable**.
4. Select the **Contents of URL** output belonging to the POST upload action.

**Contents of URL** here is an output variable, not another action to add.
Do **not** add a second **Get Contents of URL** action: that downloads the
location page as HTML instead of opening it in Safari. Do not select
**Shortcut Input** as the input to **Open URLs**.

Remove any extra **Get Contents of URL** or **Show Content** actions.
The complete shortcut should contain:

```text
Shortcut Input (CSV file)
  -> Get Contents of URL (POST /upload, File: Shortcut Input)
  -> Open URLs (output of the POST action)
```

### 5. Upload and save a batch

1. Export your scans as a CSV from **QR2CSV**.
2. Tap **Share** on the exported CSV file.
3. Choose **Upload ISBN Scans**.
4. Allow network or browser permissions if prompted.
5. Safari should open a page titled **Assign a location**.
6. Select an existing location or create one.
7. Tap **Assign location and save**.

Uploading holds one scan batch in memory. Uploading another CSV replaces the
waiting batch, and restarting the service discards it. Only **Assign location
and save** writes the book records to `isbn_list.json`. The result page lists
added, updated, and already up-to-date ISBNs. Upload the CSV again if the
waiting batch was replaced or the service restarted.

### Troubleshooting an invalid URL

If **Open URLs** reports "Make sure you pass a valid URL":

1. Temporarily insert **Show Result** immediately after the POST action.
2. Set its input to the POST action's **Contents of URL** output.
3. Run the shortcut with a CSV file and inspect the result. It should be a
   single URL like:

   ```text
   http://100.106.249.52:8082/assign-location
   ```

4. If it shows HTML beginning with `<!doctype html>`, remove any second
   **Get Contents of URL** action. If the POST itself returned HTML, read the
   error message in that response.
5. If it shows CSV content, you selected **Shortcut Input** instead of the
   POST action's output.
6. Remove **Show Result** after checking. Explicitly set **Open URLs** back
   to the POST action's **Contents of URL** output, not the temporary
   **Show Result** action's output.
