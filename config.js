// Settings for the hosted dashboard (Vercel). Not used when the dashboard is opened from the backend
// itself (http://127.0.0.1:8000 or the *.onrender.com address).
// CRAWLER_API_BASE_URL: the Render backend's address, no trailing slash. When the backend moves, change it
// here AND in frontend/config.js, push, and Vercel redeploys the dashboard. See DEPLOY.md, section 3.
window.CRAWLER_API_BASE_URL = "https://website-crawler-z8qf.onrender.com";
