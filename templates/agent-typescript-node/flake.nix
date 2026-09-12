{
  description = "Agent-ready TypeScript Node 22 environment";
  inputs = { nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable"; flake-utils.url = "github:numtide/flake-utils"; };
  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system: let pkgs = nixpkgs.legacyPackages.${system}; in {
      # nodejs_22 supplies the npm executable used by the adapter.
      devShells.default = pkgs.mkShell { packages = with pkgs; [ nodejs_22 typescript python3 just git gh jq ]; };
    });
}
