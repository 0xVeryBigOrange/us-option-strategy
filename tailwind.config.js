/** 构建：/path/to/tailwindcss -c tailwind.config.js -i src/input.css -o assets/site.css --minify */
module.exports = {
  content: ['./index.html', './assets/app.js'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans SC', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
      colors: { brand: { DEFAULT: '#ff7a1a', soft: '#ff9a4d' } },
    },
  },
};
