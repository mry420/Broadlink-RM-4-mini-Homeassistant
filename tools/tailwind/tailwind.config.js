/** Tailwind build for the Broadlink IR Manager UI.
 *  Rebuild: npx tailwindcss@3.4.19 -c tools/tailwind/tailwind.config.js \
 *           -i tools/tailwind/input.css -o broadlink_manager/app/static/tailwind.css --minify
 *  (run from the repository root) */
module.exports = {
  content: [
    "./broadlink_manager/app/templates/**/*.html",
    "./broadlink_manager/app/static/**/*.js",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ha: {
          bg: "#111318",
          card: "#1c1f26",
          elevated: "#252a33",
          border: "#2c313b",
          text: "#e6e8eb",
          muted: "#9aa1ab",
          primary: "#03a9f4",
          success: "#43a047",
          error: "#ef5350",
        },
      },
    },
  },
  plugins: [],
};
