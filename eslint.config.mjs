// The JS lint for web/static: no markup from strings, no eval, strict equality, no stray globals.
import js from "@eslint/js";
import globals from "globals";

const STATIC = "src/herdr_pwa/web/static";

export default [
  { ignores: [`${STATIC}/htmx.min.js`] },
  { files: [`${STATIC}/*.js`], ...js.configs.recommended },
  {
    files: [`${STATIC}/*.js`],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
    },
    rules: {
      eqeqeq: "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-undef": "error",
      "no-empty": ["error", { allowEmptyCatch: false }],
      // The scripts keep to ES5's catch, which needs a binding even when the error is dropped.
      "no-unused-vars": ["error", { caughtErrors: "none" }],
      "no-restricted-syntax": [
        "error",
        {
          // The pages build DOM nodes; a string parsed as markup is how a pane's text becomes script.
          selector: "AssignmentExpression > MemberExpression.left[property.name=/^(innerHTML|outerHTML)$/]",
          message: "Build nodes with createElement and textContent, never markup from a string.",
        },
        {
          selector: "CallExpression[callee.property.name='insertAdjacentHTML']",
          message: "Build nodes with createElement and textContent, never markup from a string.",
        },
      ],
    },
  },
  {
    files: [`${STATIC}/*.js`],
    ignores: [`${STATIC}/sw.js`],
    languageOptions: { globals: { ...globals.browser, htmx: "readonly" } },
  },
  { files: [`${STATIC}/sw.js`], languageOptions: { globals: globals.serviceworker } },
];
