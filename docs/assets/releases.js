// Pulls release data live from the GitHub API so the site never needs a
// manual edit when a new version ships -- download links always match
// whatever asset was actually uploaded, whatever it's named.
(function () {
  const REPO = "GhostGeorge/portkey";
  const API_BASE = `https://api.github.com/repos/${REPO}`;

  function formatDate(iso) {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  }

  function formatSize(bytes) {
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function downloadIconSvg() {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
      'stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M12 4v12"></path><path d="M6 12l6 6 6-6"></path><path d="M5 21h14"></path>' +
      "</svg>"
    );
  }

  function renderRelease(release, isLatest) {
    const assets = release.assets || [];
    const actions = assets.length
      ? assets
          .map(
            (asset) => `
            <a class="btn-secondary" href="${asset.browser_download_url}" download>
              ${downloadIconSvg()}
              ${escapeHtml(asset.name)}
            </a>
            <span class="release__size">${formatSize(asset.size)} · Windows 10/11</span>
          `
          )
          .join("")
      : `<a class="btn-secondary" href="https://github.com/${REPO}/releases/tag/${release.tag_name}">View on GitHub</a>`;

    return `
      <article class="release${isLatest ? " release--latest" : ""}">
        <div class="release__info">
          <h2>${escapeHtml(release.tag_name)}${isLatest ? ' <span class="tag">Latest</span>' : ""}</h2>
          <p class="release__date">${formatDate(release.published_at)}</p>
          <p class="release__notes">${release.body ? escapeHtml(release.body) : "No release notes provided."}</p>
        </div>
        <div class="release__action">
          ${actions}
        </div>
      </article>
    `;
  }

  async function initReleaseList() {
    const list = document.getElementById("release-list");
    if (!list) return;
    try {
      const res = await fetch(`${API_BASE}/releases`);
      if (!res.ok) throw new Error(`GitHub API returned ${res.status}`);
      const releases = (await res.json()).filter((r) => !r.draft);
      if (!releases.length) throw new Error("no releases found");
      list.innerHTML = releases.map((r, i) => renderRelease(r, i === 0)).join("");
    } catch (err) {
      list.innerHTML = `<p class="release-status">Couldn't load releases right now — see them directly on <a href="https://github.com/${REPO}/releases">GitHub</a>.</p>`;
    }
  }

  async function initHeroDownload() {
    const btn = document.getElementById("hero-download");
    const versionLabel = document.getElementById("hero-version");
    if (!btn) return;
    try {
      const res = await fetch(`${API_BASE}/releases/latest`);
      if (!res.ok) throw new Error(`GitHub API returned ${res.status}`);
      const release = await res.json();
      const asset = release.assets && release.assets[0];
      if (asset) btn.href = asset.browser_download_url;
      if (versionLabel) versionLabel.textContent = release.tag_name;
    } catch (err) {
      // Leave the button pointed at releases.html (its default href) so
      // it's still useful even if the API call fails or is rate-limited.
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    initReleaseList();
    initHeroDownload();
  });
})();
