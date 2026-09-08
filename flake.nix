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
    flake-utils.lib.eachDefaultSystem (system:
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
    };
  };
}
