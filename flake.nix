{
  description = "upiscium's env templates";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    opencodeContract = {
      url = "github:upiscium/OpencodeContract";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, opencodeContract }:
    let
      trustedRevision = if self ? rev then self.rev else "0000000000000000000000000000000000000000";
    in flake-utils.lib.eachDefaultSystem (system:
      if system == "x86_64-darwin" then { }
      else
        let
          pkgs = nixpkgs.legacyPackages.${system};
          isLinux = pkgs.stdenv.hostPlatform.isLinux;
        in {
          devShells.default = pkgs.mkShell {
            packages = with pkgs; [
              just
              python3
              git
              gh
            ];
          };
          packages.source-collaboration = pkgs.runCommand "templates-source-collaboration" {
            meta.mainProgram = "templates-source";
          } ''
            mkdir -p "$out/bin" "$out/lib/templates-source"
            substitute ${./tools/source_collaboration.py} "$out/lib/templates-source/source_collaboration.py" \
              --replace-fail '@TRUSTED_SOURCE_BASE@' '${trustedRevision}' \
              --replace-fail '@TRUSTED_GIT@' '${pkgs.git}/bin/git' \
              --replace-fail '@TRUSTED_GH@' '${pkgs.gh}/bin/gh' \
              --replace-fail '@TRUSTED_PATH@' '${nixpkgs.lib.makeBinPath [ pkgs.git pkgs.gh pkgs.openssh pkgs.python3 ]}' \
              --replace-fail '#!/usr/bin/env python3' '#!${pkgs.python3}/bin/python3 -I'
            cp ${./tools/render_templates.py} "$out/lib/templates-source/render_templates.py"
            substitute ${./tools/source_publication_launcher.sh} "$out/bin/templates-source" \
              --replace-fail '@PYTHON@' '${pkgs.python3}/bin/python3' \
              --replace-fail '@PAYLOAD@' "$out/lib/templates-source/source_collaboration.py" \
              --replace-fail '@PATH@' '${nixpkgs.lib.makeBinPath [ pkgs.git pkgs.gh pkgs.openssh pkgs.python3 ]}'
            chmod 755 "$out/lib/templates-source/source_collaboration.py"
            chmod 755 "$out/bin/templates-source"
          '';
        } // nixpkgs.lib.optionalAttrs isLinux {
          checks.opencode-contract = pkgs.runCommand "templates-opencode-contract" {
            nativeBuildInputs = [ opencodeContract.packages.${system}.opencode-contract ];
          } ''
            opencode-contract audit-consumer \
              --profile agent-core \
              --consumer ${self} \
              --strict
            touch "$out"
          '';
        })
    // {
    templates = {
      python = {
        path = ./templates/agent-python;
        description = "Compatibility alias for the Agent-ready Python + uv template";
      };
      rust = {
        path = ./templates/agent-rust;
        description = "Compatibility alias for the Agent-ready Rust template";
      };
      agent-base = {
        path = ./templates/agent-base;
        description = "Generated Agent-ready base repository scaffold";
      };
      agent-cpp-cmake = {
        path = ./templates/agent-cpp-cmake;
        description = "Generated Agent-ready C++/CMake repository scaffold";
      };
      agent-nix = {
        path = ./templates/agent-nix;
        description = "Generated Agent-ready Nix flake repository scaffold";
      };
      agent-python = {
        path = ./templates/agent-python;
        description = "Generated Agent-ready Python + uv repository scaffold";
      };
      agent-rust = {
        path = ./templates/agent-rust;
        description = "Generated Agent-ready Rust repository scaffold";
      };
      agent-typescript-node = {
        path = ./templates/agent-typescript-node;
        description = "Generated Agent-ready TypeScript + Node.js + npm repository scaffold";
      };
    };
  };
}
