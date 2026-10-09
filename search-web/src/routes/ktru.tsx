import { Navigate, useSearchParams } from "@solidjs/router";

export default function KtruRedirect() {
  const [params] = useSearchParams();
  const code = Array.isArray(params.code) ? params.code[0] : params.code;
  const query = new URLSearchParams({ tab: "ktru" });
  if (code?.trim()) query.set("code", code.trim());
  return <Navigate href={`/directories?${query}`} />;
}
