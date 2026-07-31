module.exports = function (eleventyConfig) {
  eleventyConfig.addPassthroughCopy("src/mydashboard/vendor");
  eleventyConfig.addPassthroughCopy("src/assets");
  eleventyConfig.addFilter("json", (val) => JSON.stringify(val));
  eleventyConfig.addFilter("pct", (val) => (val * 100).toFixed(2) + "%");
  eleventyConfig.addFilter("sum", (arr, key) => arr.reduce((acc, item) => acc + (item[key] || 0), 0));
  eleventyConfig.addFilter("mmddyy", (dateStr) => {
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const yy = String(d.getFullYear()).slice(-2);
    return `${mm}-${dd}-${yy}`;
  });
  eleventyConfig.addFilter("readableDateTime", function(dateObj) {
    const d = new Date(dateObj);
    if (isNaN(d.getTime())) return "unavailable, check that _data/buildTime.js exists";
    return d.toLocaleString("en-US", {
      day: "numeric", month: "long", year: "numeric",
      hour: "numeric", minute: "2-digit", timeZoneName: "short",
      timeZone: "America/New_York",
    });
  });

  return {
    dir: {
      input: "src",
      output: "_site",
      includes: "_includes",
      data: "_data",
    },
  };
};
