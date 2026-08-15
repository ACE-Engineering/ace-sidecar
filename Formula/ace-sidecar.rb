class AceSidecar < Formula
  include Language::Python::Virtualenv

  desc "Local developer observability sidecar and skill miner for Claude Code & Antigravity"
  homepage "https://github.com/ACE-Engineering/ace-sidecar"
  # The filename carries an underscore even though the project name is hyphenated:
  # PEP 625 has build backends normalise it, so .../ace-sidecar-0.1.1.tar.gz is a 404.
  url "https://files.pythonhosted.org/packages/source/a/ace-sidecar/ace_sidecar-0.1.1.tar.gz"
  sha256 "88fc9edb468f697139fbf6cccbb7a90fec7a7b9c18634076821fdbc582aaf8fc"
  license "AGPL-3.0-or-later"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/ace", "--help"
  end
end
