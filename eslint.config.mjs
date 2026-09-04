// Lint rules for the dashboard's JavaScript, after pdf.js's configuration
// (mozilla/pdf.js eslint.config.mjs) minus its project-specific rules.
import globals from "globals";
import importX from "eslint-plugin-import-x";
import js from "@eslint/js";
import noUnsanitized from "eslint-plugin-no-unsanitized";
import perfectionist from "eslint-plugin-perfectionist";
import prettierRecommended from "eslint-plugin-prettier/recommended";
import regexpPlugin from "eslint-plugin-regexp";
import unicorn from "eslint-plugin-unicorn";

const jsFiles = ["**/*.js", "**/*.mjs"];

export default [
  {
    ignores: ["node_modules/", ".venv/", "**/__pycache__/", ".claude/"],
  },

  js.configs.recommended,
  prettierRecommended,
  {
    files: jsFiles,
    plugins: regexpPlugin.configs["flat/recommended"].plugins,
    rules: {
      ...regexpPlugin.configs["flat/recommended"].rules,
      "regexp/no-octal": "error",
      "regexp/no-potentially-useless-backreference": "error",
      "regexp/no-standalone-backslash": "error",
      "regexp/no-super-linear-move": "error",
      "regexp/optimal-lookaround-quantifier": "error",
      "regexp/prefer-escape-replacement-dollar-char": "error",
      "regexp/prefer-regexp-exec": "error",
    },
  },
  {
    files: jsFiles,

    plugins: {
      import: importX.flatConfigs.recommended.plugins["import-x"],
      "no-unsanitized": noUnsanitized,
      perfectionist,
      unicorn,
    },

    settings: {
      "import-x/resolver-next": [importX.createNodeResolver()],
    },

    languageOptions: {
      globals: globals.browser,
      ecmaVersion: 2025,
      sourceType: "module",
    },

    rules: {
      "import/export": "error",
      "import/extensions": ["error", "always", { ignorePackages: true }],
      "import/first": "error",
      "import/named": "error",
      "import/no-cycle": "error",
      "import/no-empty-named-blocks": "error",
      "import/no-commonjs": "error",
      "import/no-mutable-exports": "error",
      "import/no-self-import": "error",
      "import/no-unresolved": "error",
      "no-unsanitized/method": "error",
      "no-unsanitized/property": "error",
      "perfectionist/sort-exports": "error",
      "perfectionist/sort-named-exports": "error",
      "unicorn/no-abusive-eslint-disable": "error",
      "unicorn/no-array-reduce": ["error", { allowSimpleOperations: true }],
      "unicorn/no-console-spaces": "error",
      "unicorn/no-incorrect-query-selector": "error",
      "unicorn/no-instanceof-builtins": "error",
      "unicorn/no-invalid-remove-event-listener": "error",
      "unicorn/no-multiple-promise-resolver-calls": "error",
      "unicorn/no-new-buffer": "error",
      "unicorn/no-single-promise-in-promise-methods": "error",
      "unicorn/no-typeof-undefined": ["error", { checkGlobalVariables: false }],
      "unicorn/no-unnecessary-array-flat-depth": "error",
      "unicorn/no-unnecessary-array-splice-count": "error",
      "unicorn/no-unnecessary-slice-end": "error",
      "unicorn/no-useless-collection-argument": "error",
      "unicorn/no-useless-promise-resolve-reject": "error",
      "unicorn/no-useless-spread": "error",
      "unicorn/prefer-array-find": "error",
      "unicorn/prefer-array-flat": "error",
      "unicorn/prefer-array-flat-map": "error",
      "unicorn/prefer-array-index-of": "error",
      "unicorn/prefer-array-some": "error",
      "unicorn/prefer-at": "error",
      "unicorn/prefer-class-fields": "error",
      "unicorn/prefer-classlist-toggle": "error",
      "unicorn/prefer-date-now": "error",
      "unicorn/prefer-dom-node-append": "error",
      "unicorn/prefer-dom-node-remove": "error",
      "unicorn/prefer-import-meta-properties": "error",
      "unicorn/prefer-includes": "error",
      "unicorn/logical-assignment-operators": [
        "error",
        "always",
        { enforceForIfStatements: true },
      ],
      "unicorn/prefer-logical-operator-over-ternary": "error",
      "unicorn/prefer-modern-dom-apis": "error",
      "unicorn/prefer-modern-math-apis": "error",
      "unicorn/prefer-negative-index": "error",
      "unicorn/prefer-optional-catch-binding": "error",
      "unicorn/prefer-regexp-test": "error",
      "unicorn/prefer-single-call": "error",
      "unicorn/prefer-string-replace-all": "error",
      "unicorn/prefer-string-starts-ends-with": "error",
      "unicorn/prefer-ternary": ["error", "only-single-line"],
      "unicorn/throw-new-error": "error",

      // Possible errors
      "no-cond-assign": ["error", "except-parens"],
      "no-constant-condition": ["error", { checkLoops: false }],
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-inner-declarations": ["error", "functions"],
      "no-promise-executor-return": "error",
      "no-template-curly-in-string": "error",
      "no-unsafe-optional-chaining": [
        "error",
        { disallowArithmeticOperators: true },
      ],
      "use-isnan": ["error", { enforceForIndexOf: true }],
      "valid-typeof": ["error", { requireStringLiterals: true }],

      // Best practices
      "accessor-pairs": [
        "error",
        { setWithoutGet: true, enforceForClassMembers: true },
      ],
      "consistent-return": "error",
      curly: ["error", "all"],
      "default-case-last": "error",
      "dot-notation": "error",
      eqeqeq: ["error", "always", { null: "ignore" }],
      "grouped-accessor-pairs": ["error", "getBeforeSet"],
      "no-alert": "error",
      "no-caller": "error",
      "no-else-return": "error",
      "no-eval": "error",
      "no-extend-native": "error",
      "no-extra-bind": "error",
      "no-extra-label": "error",
      "no-implied-eval": "error",
      "no-iterator": "error",
      "no-lone-blocks": "error",
      "no-lonely-if": "error",
      "no-multi-str": "error",
      "no-new": "error",
      "no-new-func": "error",
      "no-new-wrappers": "error",
      "no-octal-escape": "error",
      "no-return-await": "error",
      "no-self-compare": "error",
      "no-throw-literal": "error",
      "no-unused-expressions": "error",
      "no-useless-call": "error",
      "no-useless-concat": "error",
      "no-useless-return": "error",
      "prefer-object-has-own": "error",
      "prefer-promise-reject-errors": "error",
      "prefer-spread": "error",
      radix: "error",
      "wrap-iife": ["error", "any"],
      yoda: ["error", "never", { exceptRange: true }],

      // Variables
      "no-label-var": "error",
      "no-shadow": "error",
      "no-undef-init": "error",
      "no-undef": ["error", { typeof: true }],
      "no-unused-vars": ["error", { vars: "all", args: "none" }],
      "no-use-before-define": [
        "error",
        { functions: false, classes: false, variables: false },
      ],

      // Style (formatting itself is Prettier's)
      "lines-between-class-members": ["error", "always"],
      "max-len": ["error", { code: 1000, comments: 80, ignoreUrls: true }],
      "new-cap": ["error", { newIsCap: true, capIsNew: false }],
      "no-array-constructor": "error",
      "no-multiple-empty-lines": ["error", { max: 1, maxEOF: 0, maxBOF: 1 }],
      "no-nested-ternary": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "BinaryExpression[operator='instanceof'][right.name='Object']",
          message: "Use `typeof` rather than `instanceof Object`.",
        },
        {
          selector: "MemberExpression[property.name='hasOwnProperty']",
          message:
            "Use `Object.hasOwn` rather than `Object.prototype.hasOwnProperty`.",
        },
      ],
      "no-unneeded-ternary": "error",
      "operator-assignment": "error",
      "prefer-exponentiation-operator": "error",
      "spaced-comment": ["error", "always", { block: { balanced: true } }],

      // ES2015+
      "arrow-body-style": ["error", "as-needed"],
      "no-duplicate-imports": "error",
      "no-useless-computed-key": "error",
      "no-useless-constructor": "error",
      "no-useless-rename": "error",
      "no-var": "error",
      "object-shorthand": ["error", "always", { avoidQuotes: true }],
      "prefer-const": "error",
      "sort-imports": ["error", { ignoreCase: true }],
      "template-curly-spacing": ["error", "never"],
    },
  },
  {
    files: ["**/icons.js"],
    rules: { "max-len": "off" }, // SVG path data
  },
  {
    files: ["eslint.config.mjs"],
    languageOptions: { globals: globals.node },
  },
];
