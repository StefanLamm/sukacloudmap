export type LocationValue={longitude:string;latitude:string;crs:string;location_source:string;address?:string};

// Search results contain POI names and metadata, which are not business fields.
export function locationFields({longitude,latitude,crs,location_source,address}:LocationValue):LocationValue{
  return {longitude,latitude,crs,location_source,...(address!==undefined?{address}:{})};
}
