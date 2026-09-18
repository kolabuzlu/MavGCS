## Installing & Running MavGCS

### Windows

Download `MavGCS-<version>-windows.zip` from the
[Releases page](https://github.com/kolabuzlu/MavGCS/releases), extract it,
and run **MavGCS.exe**. No setup needed.

### macOS

Download `MavGCS-<version>-macos-x86_64.zip` from the
[Releases page](https://github.com/kolabuzlu/MavGCS/releases) and drag
**MavGCS.app** to your Applications folder.

There is one file and it fits every Mac. It is an Intel build: native on
an Intel Mac, and on Apple Silicon (M1 and later) macOS runs it through
Rosetta, which it offers to install the first time you open the app.

#### "MavGCS is damaged and can't be opened"

It isn't damaged. MavGCS is not signed with an Apple Developer
certificate, and macOS puts every unsigned app it downloads into
quarantine. "Damaged" is simply the wrong message for it.

Clear the quarantine flag once:

```
xattr -dr com.apple.quarantine /Applications/MavGCS.app
```

Then open it normally. You only need to do this once per download - not
every launch - and it does nothing except remove the download marker
macOS attached to the file.

If you would rather not use Terminal: right-click the app and choose
**Open**, then confirm at the prompt. That works on some macOS versions
and not others, which is why the command above is given first.
