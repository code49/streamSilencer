{ pkgs ? import <nixpkgs> {} }:

let
  pythonEnv = pkgs.python3.withPackages (ps: with ps; []);
in
pkgs.mkShell {
  buildInputs = [
    pythonEnv
    pkgs.wireplumber
    pkgs.pipewire
  ];

  shellHook = ''
    echo "========================================================="
    echo " Stream Silencer Development Environment Loaded (NixOS)"
    echo "========================================================="
    echo "Python: $(python --version)"
    echo ""
    echo "Run 'python stream_silencer.py --help' to view options."
    echo "Run 'python stream_silencer.py --status' to inspect streams."
    echo "========================================================="
  '';
}
