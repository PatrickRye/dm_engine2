/** @type {import('jest').Config} */
module.exports = {
  testEnvironment: "jsdom",
  testMatch: ["**/*.test.js"],
  setupFilesAfterEnv: ["./setup.js"],
  testTimeout: 10000,
  // Keep module transform simple — client code is plain ES2020 JS
  transform: {},
  // Allow importing .js files without extension
  moduleFileExtensions: ["js", "json"],
  // Root dir for module resolution
  roots: ["<rootDir>"],
};
