// ARACHNIX Companion's service worker. The extension does its work in the popup and the options page, on the user's
// clicks; the only background job is to open the options page once after installing, so the server can be set up.

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install') chrome.runtime.openOptionsPage();
});
