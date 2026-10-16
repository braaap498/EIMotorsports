"use client";

import { useParams } from "next/navigation";
import ProductForm from "../../../../components/admin/ProductForm";

export default function EditProductPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  if (!Number.isInteger(id)) return <p className="text-sm text-red-600">Invalid product id.</p>;
  return <ProductForm key={id} productId={id} />;
}
