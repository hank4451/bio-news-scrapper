export default {
  async scheduled(event, env) {
    const response = await fetch(
      `https://api.github.com/repos/${env.REPOSITORY}/actions/workflows/${env.WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "bio-news-trigger",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );
    if (!response.ok) {
      throw new Error(`workflow dispatch failed ${response.status}: ${await response.text()}`);
    }
  },
};
