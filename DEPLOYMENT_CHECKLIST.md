# Deployment Checklist

Tick each box in order. Details are in README.md.

## Before pushing
- [ ] `pytest -q` shows `102 passed`
- [ ] `python check_setup.py --daraja` shows `all good` and `Daraja Sandbox login: OK`
- [ ] `.env` exists locally and is **not** listed by `git status`
- [ ] `bash check_secrets.sh` prints `OK`
- [ ] No real credentials in `.env.example`, `render.yaml` or any file in `git ls-files`

## Neon (database)
- [ ] Project created in the same region you will use on Render
- [ ] Connection string saved privately (password manager or `.env`)
- [ ] `python check_setup.py` says `Database: reachable`

## GitHub
- [ ] Private repository created, code pushed to `main`
- [ ] Fine-grained token limited to this repo (or SSH key)

## Render
- [ ] Web service created from the repo, **Free** instance type
- [ ] Build: `pip install -r requirements.txt`
- [ ] Start: `gunicorn server:app --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT`
- [ ] Health check path `/api/test`
- [ ] Environment variables set: `APP_ENV=production`, `DATABASE_URL`, `DARAJA_CONSUMER_KEY`,
      `DARAJA_CONSUMER_SECRET`, `DARAJA_SHORTCODE`, `DARAJA_PASSKEY`, `CALLBACK_TOKEN`,
      `DARAJA_CALLBACK_URL`, `MAX_AMOUNT`, `TRUSTED_PROXY_HOPS`
- [ ] Real hostname read from the Render page
- [ ] `DARAJA_CALLBACK_URL` = `https://<real-host>/api/callback/<CALLBACK_TOKEN>`
- [ ] Service redeployed after the last variable change

## Verify the public site
- [ ] `curl https://HOST/api/test?deep=1` -> `configured:true`, `database:ok`
- [ ] GET on callback URL -> 405; wrong token -> 403; junk body -> 400; unknown ID -> 200 (README step 20)
- [ ] Page loads over HTTPS and validation messages work

## Sandbox payment tests (real Daraja)
- [ ] Cancel -> page shows cancelled; logs show `code=1032 outcome=updated`
- [ ] Success -> page shows success with receipt; DB row is `SUCCESS`
- [ ] Failure/timeout -> page shows failed with reason
- [ ] Duplicate callback (if any) logged as `outcome=duplicate`, result unchanged
- [ ] Neon SQL Editor shows the rows; `phone_masked` is masked
- [ ] Render "Deploy latest commit", then old payment status is unchanged

## Wrap-up
- [ ] Logs contain no credentials or full phone numbers
- [ ] Limitations in README section 14 read and understood
- [ ] When finished: delete Render service / Neon project / token (README section 15)
