Deploy the Daily Market Report to GitHub Pages.

Project directory: /Users/jackjensen/Documents/Claude/Projects/Daily Report
Repo: jackjensen0614/daily-market-report
Live URL: https://jackjensen0614.github.io/daily-market-report/

Steps:

1. Run `git -C "/Users/jackjensen/Documents/Claude/Projects/Daily Report" status --short` to see what's uncommitted.

2. If there are uncommitted changes:
   - Stage modified tracked files: `git -C "/Users/jackjensen/Documents/Claude/Projects/Daily Report" add -u`
   - Commit: `git -C "/Users/jackjensen/Documents/Claude/Projects/Daily Report" commit -m "deploy: <one-line summary of what changed>"`
   - Then push.

3. Push to main: `git -C "/Users/jackjensen/Documents/Claude/Projects/Daily Report" push`

4. The push triggers GitHub Actions automatically (push trigger on main). Poll for the new run every 15 seconds:
   ```
   curl -s "https://api.github.com/repos/jackjensen0614/daily-market-report/actions/runs?per_page=3&branch=main"
   ```
   Parse the JSON response: find the run whose `head_sha` matches the commit we just pushed (get it via `git -C "..." rev-parse HEAD`). Wait until its `status` is `completed`. Report `conclusion` (success/failure).

5. Tell the user the deployment is live at https://jackjensen0614.github.io/daily-market-report/ and summarize what was deployed.

If there are no uncommitted changes and the latest run already reflects the current HEAD, skip the push and just report the current deployment status.
