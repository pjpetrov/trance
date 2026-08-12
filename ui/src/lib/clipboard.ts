/** Copy that works where the Clipboard API does not.
 *
 * navigator.clipboard exists only in secure contexts — https, or localhost.
 * trance is reached over the LAN as plain http, where it is undefined, and
 * the share button's "copied!" crashed instead of copying. The fallback is
 * the old textarea trick; when even that fails, the caller still has the
 * text and can show it.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the textarea path
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  } catch {
    return false;
  }
}
