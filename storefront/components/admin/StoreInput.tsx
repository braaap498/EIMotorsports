"use client";

import { useEffect, useId, useState } from "react";
import { api } from "../../lib/adminApi";
import { inputCls } from "./ui";

/* The eBay store box used on every eBay tab: a text box that offers the stores already on the
   Live listings tab (pick one from the drop-down, or type any name). */
export default function StoreInput({
  value,
  onChange,
  placeholder,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const listId = useId();
  const [stores, setStores] = useState<string[]>([]);

  useEffect(() => {
    api<{ stores: { store: string }[] }>("/ebay-listings/stores")
      .then((r) => setStores(r.stores.map((s) => s.store)))
      .catch(() => {}); // the box still works as a plain text box
  }, []);

  return (
    <>
      <input
        className={inputCls}
        list={listId}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
      />
      <datalist id={listId}>
        {stores.map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>
    </>
  );
}
