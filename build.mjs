import * as esbuild from "esbuild";

const watch = process.argv.includes("--watch");

const externalUrls = {
  name: "external-urls",
  setup(build) {
    build.onResolve({ filter: /^https?:\/\// }, (args) => ({
      path: args.path,
      external: true,
    }));
  },
};

const config = {
  entryPoints: {
    "editor.bundle": "projects/static/projects/js/editor/main.js",
  },
  bundle: true,
  format: "esm",
  target: ["es2022"],
  outdir: "projects/static/projects/js/editor/dist",
  entryNames: "[name]",
  sourcemap: true,
  minify: !watch,
  logLevel: "info",
  plugins: [externalUrls],
};

if (watch) {
  const ctx = await esbuild.context(config);
  await ctx.watch();
  console.log("esbuild watching…");
} else {
  await esbuild.build(config);
}
