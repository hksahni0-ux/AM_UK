// Drives AM_UK's GitHub Actions workflow on a schedule, since GitHub's own
// `schedule` trigger proved unreliable (didn't fire at all for hours after
// being added to a new repo/workflow). Google Apps Script's time-based
// triggers are far more dependable.
//
// Setup (one-time):
//   1. Project Settings (gear icon) -> Script Properties -> add:
//        GITHUB_PAT = <fine-grained PAT, Actions: Read and write, scoped to
//                      hksahni0-ux/AM_UK only>
//   2. Run `installTrigger` once from this editor (Run button, function
//      dropdown) and approve the Google authorization prompt.
//   3. Done. `runEmailRunnerIfDue` will now fire every ~15 minutes; it only
//      calls GitHub when the current UK time falls inside a real send window,
//      so most invocations cost nothing on the GitHub Actions side.

var GITHUB_OWNER = 'hksahni0-ux';
var GITHUB_REPO = 'AM_UK';
var WORKFLOW_FILE = 'email_runner.yml';
var GITHUB_REF = 'main';

// Mirrors MORNING_WINDOW / FRIDAY_WINDOW in config/settings.py.
function isWithinSendWindow_() {
  var tz = 'Europe/London';
  var now = new Date();
  var day = Utilities.formatDate(now, tz, 'EEEE');
  var hour = parseInt(Utilities.formatDate(now, tz, 'H'), 10);
  var minute = parseInt(Utilities.formatDate(now, tz, 'm'), 10);
  var minutesNow = hour * 60 + minute;

  if (['Monday', 'Tuesday', 'Wednesday', 'Thursday'].indexOf(day) !== -1) {
    return minutesNow >= 10 * 60 && minutesNow <= 16 * 60; // 10:00-16:00
  }
  if (day === 'Friday') {
    return minutesNow >= 8 * 60 + 30 && minutesNow <= 12 * 60 + 30; // 08:30-12:30
  }
  return false;
}

function dispatchGithubWorkflow_() {
  var pat = PropertiesService.getScriptProperties().getProperty('GITHUB_PAT');
  if (!pat) {
    Logger.log('GITHUB_PAT script property is not set — see setup notes at top of file.');
    return;
  }
  var url = 'https://api.github.com/repos/' + GITHUB_OWNER + '/' + GITHUB_REPO +
    '/actions/workflows/' + WORKFLOW_FILE + '/dispatches';
  var response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + pat,
      Accept: 'application/vnd.github+json'
    },
    payload: JSON.stringify({ ref: GITHUB_REF }),
    muteHttpExceptions: true
  });
  Logger.log('GitHub dispatch response: %s %s', response.getResponseCode(), response.getContentText());
}

function runEmailRunnerIfDue() {
  if (isWithinSendWindow_()) {
    dispatchGithubWorkflow_();
  } else {
    Logger.log('Outside send window, skipping.');
  }
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runEmailRunnerIfDue') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('runEmailRunnerIfDue')
    .timeBased()
    .everyMinutes(15)
    .create();
  Logger.log('Trigger installed — runEmailRunnerIfDue will fire every ~15 minutes.');
}
