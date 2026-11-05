# ChatgptXBlock sandbox preview

Static browser preview of the XBlock UI. Serves the real files from
`chatgptxblock/static/` (HTML template, CSS, block JS, vendored
`marked`/`DOMPurify`) with the `get_answer` / `get_conversation` /
`reset_conversation` handlers mocked — no OpenAI call, no Open edX needed.

## Run

    docker run -d --name chatgpt-xblock-sandbox -p 8125:80 \
      -v "$(pwd):/usr/share/nginx/html:ro" nginx:alpine

Open http://localhost:8125/sandbox/index.html

Stop: `docker rm -f chatgpt-xblock-sandbox`

(Any static file server pointed at the repo root works too, e.g.
`python3 -m http.server 8125` from the repo root.)

## Notes

- jQuery is loaded from CDN, so the browser needs internet access.
  In the real LMS the platform provides jQuery.
- `sandbox/index.html` fetches the real HTML template and fills the
  `{disclaimer}` / `{self.description}` / `{reflection}` placeholders the same
  way `chatgptxblock.py student_view()` does, then loads the unmodified
  `static/js/src/chatgptxblock.js` against a mock runtime.
- Canned answers rotate through two markdown samples to exercise the
  markdown rendering pipeline.
- The conversation is seeded with one prior exchange so you can test the
  "New conversation" button and its confirm bar right away; confirming the
  reset clears the mock history (a page reload then shows the empty state).
  Set `SANDBOX_SEED_HISTORY = []` in `sandbox/index.html` to start empty.
